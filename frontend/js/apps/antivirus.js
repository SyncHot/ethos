/* ═══════════════════════════════════════════════════════════
   EthOS — Antivirus (ClamAV)
   ═══════════════════════════════════════════════════════════ */

AppRegistry['antivirus'] = function (appDef) {
    function esc(s) {
        if (!s && s !== 0) return '';
        const d = document.createElement('div');
        d.textContent = String(s);
        return d.innerHTML;
    }

    const win = createWindow('antivirus', {
        title: t('Antywirus (ClamAV)'),
        icon: appDef.icon || 'fa-shield-virus',
        iconColor: appDef.color || '#16a34a',
        width: 820,
        height: 640,
        resizable: true,
        maximizable: true,
    });
    const body = win.body;
    body.style.cssText = 'display:flex;flex-direction:column;height:100%;overflow:hidden;background:var(--window-bg)';

    body.innerHTML = `
        <!-- Status bar -->
        <div class="pwr-status-bar">
            <div class="pwr-status-chip" id="av-state-chip">
                <i class="fas fa-shield-virus" style="color:#16a34a"></i>
                <span>${t('Ładowanie...')}</span>
            </div>
            <div class="pwr-status-chip" id="av-db-chip" style="display:none">
                <i class="fas fa-database" style="color:var(--text-muted)"></i>
                <span id="av-db-text"></span>
            </div>
            <div class="pwr-status-chip" id="av-scanning-chip" style="display:none">
                <i class="fas fa-spinner fa-spin" style="color:#16a34a"></i>
                <span>${t('Skanowanie...')}</span>
            </div>
        </div>

        <!-- Scrollable body -->
        <div class="pwr-scroll" id="av-scroll">

            <!-- Engine card -->
            <div class="pwr-card" id="av-engine-card" style="display:none">
                <div class="pwr-card-header">
                    <i class="fas fa-shield-virus" style="color:#16a34a"></i>
                    <div style="flex:1">
                        <div class="pwr-card-title">ClamAV</div>
                        <div class="pwr-card-sub" id="av-version-sub"></div>
                    </div>
                    <button id="av-update-db-btn" class="app-btn app-btn-sm">
                        <i class="fas fa-rotate"></i> ${t('Aktualizuj bazę')}
                    </button>
                    <button id="av-uninstall-btn" class="app-btn app-btn-sm" style="color:var(--danger,#ef4444)" title="${t('Odinstaluj ClamAV')}">
                        <i class="fas fa-trash"></i>
                    </button>
                </div>
                <!-- DB update progress -->
                <div id="av-updatedb-wrap" style="display:none;padding:0 16px 14px">
                    <div class="av-prog-bar"><div class="av-prog-fill" id="av-updatedb-fill"></div></div>
                    <div class="av-prog-msg" id="av-updatedb-msg"></div>
                </div>
            </div>

            <!-- Install screen (shown when not installed) -->
            <div class="pwr-card" id="av-install-card" style="display:none">
                <div class="pwr-card-header">
                    <i class="fas fa-shield-virus" style="color:#16a34a"></i>
                    <div style="flex:1">
                        <div class="pwr-card-title">${t('Antywirus nie zainstalowany')}</div>
                        <div class="pwr-card-sub">${t('ClamAV to darmowy silnik antywirusowy open-source.')}</div>
                    </div>
                </div>
                <div style="padding:16px;display:flex;flex-direction:column;gap:12px;align-items:flex-start">
                    <div style="font-size:12px;color:var(--text-muted);background:var(--bg-default);border:1px solid var(--border);border-radius:6px;padding:5px 12px;font-family:monospace">
                        clamav, clamav-freshclam
                    </div>
                    <button id="av-install-btn" class="app-btn app-btn-primary" style="background:#16a34a;border-color:#16a34a">
                        <i class="fas fa-download"></i> ${t('Zainstaluj ClamAV')}
                    </button>
                    <div id="av-install-wrap" style="display:none;width:100%">
                        <div class="av-prog-bar"><div class="av-prog-fill" id="av-install-fill"></div></div>
                        <div class="av-prog-msg" id="av-install-msg"></div>
                    </div>
                </div>
            </div>

            <!-- Scan card -->
            <div class="pwr-card" id="av-scan-card" style="display:none">
                <div class="pwr-card-header">
                    <i class="fas fa-magnifying-glass" style="color:#16a34a"></i>
                    <div style="flex:1">
                        <div class="pwr-card-title">${t('Skanowanie na żądanie')}</div>
                        <div class="pwr-card-sub">${t('Skanuj wybrany katalog w poszukiwaniu zagrożeń')}</div>
                    </div>
                </div>
                <div style="padding:0 16px 14px;display:flex;flex-direction:column;gap:10px">
                    <div style="display:flex;gap:8px;align-items:center">
                        <input id="av-scan-path" class="app-input" style="flex:1" value="/home" placeholder="/home">
                        <button id="av-browse-btn" class="app-btn app-btn-sm" title="${t('Wybierz folder')}">
                            <i class="fas fa-folder-open"></i>
                        </button>
                        <button id="av-scan-btn" class="app-btn app-btn-primary" style="background:#16a34a;border-color:#16a34a;white-space:nowrap">
                            <i class="fas fa-play"></i> ${t('Skanuj')}
                        </button>
                        <button id="av-cancel-btn" class="app-btn app-btn-danger" style="display:none;white-space:nowrap">
                            <i class="fas fa-stop"></i> ${t('Zatrzymaj')}
                        </button>
                    </div>
                    <!-- Progress -->
                    <div id="av-scan-progress" style="display:none">
                        <div class="av-prog-bar"><div class="av-prog-fill av-prog-indeterminate" id="av-prog-fill"></div></div>
                        <div style="display:flex;justify-content:space-between;align-items:center;margin-top:6px">
                            <span class="av-prog-msg" id="av-prog-msg"></span>
                            <span id="av-prog-stats" style="font-size:12px;color:var(--text-muted);white-space:nowrap"></span>
                        </div>
                        <div id="av-threats-live" style="max-height:100px;overflow-y:auto;margin-top:4px"></div>
                    </div>
                </div>
            </div>

            <!-- Last scan result card -->
            <div class="pwr-card" id="av-last-scan-card" style="display:none">
                <div class="pwr-card-header">
                    <i id="av-last-icon" class="fas fa-check-circle" style="color:#16a34a"></i>
                    <div style="flex:1">
                        <div class="pwr-card-title">${t('Ostatnie skanowanie')}</div>
                        <div class="pwr-card-sub" id="av-last-sub"></div>
                    </div>
                    <span id="av-last-badge"></span>
                </div>
                <div id="av-last-body" style="padding:0 16px 14px"></div>
            </div>

            <!-- Schedules card -->
            <div class="pwr-card" id="av-sched-card" style="display:none">
                <div class="pwr-card-header">
                    <i class="fas fa-clock" style="color:#6366f1"></i>
                    <div style="flex:1">
                        <div class="pwr-card-title">${t('Zaplanowane skany')}</div>
                        <div class="pwr-card-sub">${t('Automatyczne skanowanie według harmonogramu')}</div>
                    </div>
                    <button id="av-add-sched-btn" class="app-btn app-btn-sm app-btn-primary">
                        <i class="fas fa-plus"></i> ${t('Dodaj')}
                    </button>
                </div>
                <div id="av-sched-list" style="padding:0 16px 14px"></div>
            </div>

            <!-- History card -->
            <div class="pwr-card" id="av-history-card" style="display:none">
                <div class="pwr-card-header">
                    <i class="fas fa-history" style="color:var(--text-muted)"></i>
                    <div style="flex:1">
                        <div class="pwr-card-title">${t('Historia skanowań')}</div>
                    </div>
                </div>
                <div id="av-history-list" style="padding:0 16px 14px"></div>
            </div>

        </div><!-- end .pwr-scroll -->

        <!-- Schedule modal -->
        <div id="av-sched-overlay" class="pwr-overlay" style="position:absolute">
            <div class="pwr-dialog" style="width:460px">
                <div class="pwr-dialog-header">
                    <i class="fas fa-clock" style="color:#6366f1"></i>
                    <span id="av-dlg-title">${t('Nowy harmonogram')}</span>
                    <button class="pwr-icon-btn" id="av-dlg-x"><i class="fas fa-times"></i></button>
                </div>
                <div class="pwr-dialog-body">
                    <div>
                        <label style="display:block;font-size:12px;color:var(--text-muted);margin-bottom:4px">${t('Nazwa')}</label>
                        <input id="av-f-name" type="text" class="app-input" style="width:100%;box-sizing:border-box" placeholder="${t('np. Codzienny skan')}">
                    </div>
                    <div>
                        <label style="display:block;font-size:12px;color:var(--text-muted);margin-bottom:4px">${t('Katalog do skanowania')}</label>
                        <div style="display:flex;gap:6px">
                            <input id="av-f-path" type="text" class="app-input" style="flex:1" value="/home">
                            <button id="av-f-browse" class="app-btn app-btn-sm" type="button" title="${t('Wybierz folder')}">
                                <i class="fas fa-folder-open"></i>
                            </button>
                        </div>
                    </div>
                    <div>
                        <label style="display:block;font-size:12px;color:var(--text-muted);margin-bottom:4px">${t('Harmonogram')}</label>
                        <select id="av-f-preset" class="app-input" style="width:100%;box-sizing:border-box">
                            <option value="0 * * * *">${t('Co godzinę')}</option>
                            <option value="0 2 * * *">${t('Codziennie o 2:00')}</option>
                            <option value="0 2 * * 0" selected>${t('Co niedzielę o 2:00')}</option>
                            <option value="0 2 1 * *">${t('Co miesiąc 1. o 2:00')}</option>
                            <option value="custom">${t('Własny cron…')}</option>
                        </select>
                    </div>
                    <div id="av-f-custom-row" style="display:none">
                        <label style="display:block;font-size:12px;color:var(--text-muted);margin-bottom:4px">${t('Wyrażenie cron (min godz dzień mies dzień_tyg)')}</label>
                        <input id="av-f-cron" type="text" class="app-input" style="width:100%;box-sizing:border-box" placeholder="0 2 * * 0">
                    </div>
                    <label style="display:flex;align-items:center;gap:8px;cursor:pointer;font-size:13px;color:var(--text-primary)">
                        <input type="checkbox" id="av-f-enabled" checked>
                        ${t('Aktywny')}
                    </label>
                </div>
                <div class="pwr-dialog-footer">
                    <button class="pwr-btn-ghost" id="av-dlg-cancel">${t('Anuluj')}</button>
                    <button class="pwr-btn-primary" id="av-dlg-save" style="background:#16a34a">
                        <i class="fas fa-check"></i> ${t('Zapisz')}
                    </button>
                </div>
            </div>
        </div>
    `;

    // ── Element refs ──────────────────────────────────────────
    const stateChip    = body.querySelector('#av-state-chip');
    const dbChip       = body.querySelector('#av-db-chip');
    const scanningChip = body.querySelector('#av-scanning-chip');
    const scanOverlay  = body.querySelector('#av-sched-overlay');

    // ── Helpers ───────────────────────────────────────────────
    function fmtDate(iso) {
        if (!iso) return '—';
        try { return new Date(iso).toLocaleString('pl-PL'); } catch { return iso; }
    }
    function fmtDuration(sec) {
        if (!sec) return '—';
        if (sec < 60) return sec.toFixed(1) + ' s';
        return Math.floor(sec / 60) + ' min ' + Math.round(sec % 60) + ' s';
    }
    function cronLabel(expr) {
        const map = {
            '0 * * * *': t('Co godzinę'),
            '0 2 * * *': t('Codziennie o 2:00'),
            '0 2 * * 0': t('Co niedzielę o 2:00'),
            '0 2 1 * *': t('Co miesiąc 1. o 2:00'),
        };
        return map[expr] || expr;
    }

    // ══════════════════════════════════════════════════════════
    // LOAD STATUS
    // ══════════════════════════════════════════════════════════

    async function loadAll() {
        const data = await api('/antivirus/status');

        if (data.error) {
            stateChip.innerHTML = `<i class="fas fa-circle-exclamation" style="color:#ef4444"></i> <span>${t('Błąd')}</span>`;
            return;
        }

        if (!data.installed) {
            // Show install card only
            stateChip.innerHTML = `<i class="fas fa-circle-xmark" style="color:#ef4444"></i> <span>${t('Nie zainstalowany')}</span>`;
            body.querySelector('#av-install-card').style.display = '';
            return;
        }

        // Update status bar
        stateChip.innerHTML = `<i class="fas fa-shield-virus" style="color:#16a34a"></i> <span style="color:#16a34a">ClamAV</span>`;
        if (data.db_info) {
            dbChip.style.display = 'flex';
            body.querySelector('#av-db-text').textContent = t('Baza') + ': ' + data.db_info;
        }
        if (data.scanning) {
            scanningChip.style.display = 'flex';
        }

        // Engine card
        body.querySelector('#av-engine-card').style.display = '';
        body.querySelector('#av-version-sub').textContent = data.version || 'ClamAV';

        // Scan card
        body.querySelector('#av-scan-card').style.display = '';

        if (data.scanning && data.active_scan) {
            const scanBtn   = body.querySelector('#av-scan-btn');
            const cancelBtn = body.querySelector('#av-cancel-btn');
            const pathInput = body.querySelector('#av-scan-path');
            const progDiv   = body.querySelector('#av-scan-progress');
            const progMsg   = body.querySelector('#av-prog-msg');
            scanBtn.disabled = true;
            scanBtn.innerHTML = `<i class="fas fa-spinner fa-spin"></i> ${t('Skanowanie...')}`;
            pathInput.disabled = true;
            pathInput.value = data.active_scan.path || '/home';
            cancelBtn.style.display = '';
            progDiv.style.display = '';
            progMsg.textContent = t('Skanowanie') + ': ' + (data.active_scan.path || '');
            attachScanListener(data.active_scan.scan_id);
        }

        // Last scan card
        if (data.last_scan) {
            renderLastScan(data.last_scan);
        }

        // Schedules card
        body.querySelector('#av-sched-card').style.display = '';
        loadSchedules();

        // History card
        body.querySelector('#av-history-card').style.display = '';
        loadHistory();
    }

    // ══════════════════════════════════════════════════════════
    // SCAN
    // ══════════════════════════════════════════════════════════

    function attachScanListener(knownScanId) {
        const progDiv    = body.querySelector('#av-scan-progress');
        const progMsg    = body.querySelector('#av-prog-msg');
        const threatsLive = body.querySelector('#av-threats-live');
        const scanBtn    = body.querySelector('#av-scan-btn');
        const cancelBtn  = body.querySelector('#av-cancel-btn');
        const pathInput  = body.querySelector('#av-scan-path');

        if (progDiv) progDiv.style.display = '';
        scanningChip.style.display = 'flex';

        const handler = (ev) => {
            if (knownScanId && ev.scan_id !== knownScanId) return;
            if (progMsg) progMsg.textContent = ev.message || '';
            const statsEl = body.querySelector('#av-prog-stats');
            if (statsEl && ev.scanned !== undefined) {
                statsEl.innerHTML = `<i class="fas fa-file" style="margin-right:2px"></i>${ev.scanned || 0} &nbsp; <i class="fas fa-bug" style="margin-right:2px;color:${(ev.threats || 0) > 0 ? '#ef4444' : 'inherit'}"></i>${ev.threats || 0}`;
            }
            if (ev.stage === 'threat' && ev.threat) {
                const d = document.createElement('div');
                d.className = 'av-threat-item';
                d.innerHTML = `<i class="fas fa-bug" style="color:#ef4444;font-size:10px"></i> <span style="font-family:monospace;font-size:11px">${esc(ev.threat)}</span>`;
                if (threatsLive) threatsLive.prepend(d);
            }
            if (ev.stage === 'done' || ev.stage === 'error') {
                if (NAS.socket) NAS.socket.off('antivirus_scan', handler);
                if (ev.stage === 'error') toast(ev.message || t('Błąd skanowania'), 'error');
                if (scanBtn) { scanBtn.disabled = false; scanBtn.innerHTML = `<i class="fas fa-play"></i> ${t('Skanuj')}`; }
                if (pathInput) pathInput.disabled = false;
                if (cancelBtn) cancelBtn.style.display = 'none';
                if (progDiv) progDiv.style.display = 'none';
                scanningChip.style.display = 'none';
                if (ev.stage === 'done') {
                    if (ev.result) renderLastScan(ev.result);
                    loadHistory();
                }
            }
        };
        if (NAS.socket) NAS.socket.on('antivirus_scan', handler);
    }

    async function startScan(scanPath) {
        const scanBtn    = body.querySelector('#av-scan-btn');
        const cancelBtn  = body.querySelector('#av-cancel-btn');
        const pathInput  = body.querySelector('#av-scan-path');
        const progDiv    = body.querySelector('#av-scan-progress');
        const progMsg    = body.querySelector('#av-prog-msg');
        const threatsLive = body.querySelector('#av-threats-live');

        scanBtn.disabled = true;
        scanBtn.innerHTML = `<i class="fas fa-spinner fa-spin"></i> ${t('Skanowanie...')}`;
        pathInput.disabled = true;
        cancelBtn.style.display = '';
        progDiv.style.display = '';
        progMsg.textContent = t('Uruchamianie...');
        threatsLive.innerHTML = '';

        const r = await api('/antivirus/scan', { method: 'POST', body: { path: scanPath } });
        if (r.error) {
            toast(r.error, 'error');
            scanBtn.disabled = false;
            scanBtn.innerHTML = `<i class="fas fa-play"></i> ${t('Skanuj')}`;
            pathInput.disabled = false;
            cancelBtn.style.display = 'none';
            progDiv.style.display = 'none';
            return;
        }
        progMsg.textContent = t('Skanowanie') + ': ' + scanPath;
        attachScanListener(r.scan_id);
    }

    // ══════════════════════════════════════════════════════════
    // LAST SCAN
    // ══════════════════════════════════════════════════════════

    function renderLastScan(scan) {
        const card = body.querySelector('#av-last-scan-card');
        card.style.display = '';
        const clean = scan.status === 'clean';
        body.querySelector('#av-last-icon').className = clean
            ? 'fas fa-check-circle' : 'fas fa-bug';
        body.querySelector('#av-last-icon').style.color = clean ? '#16a34a' : '#ef4444';
        body.querySelector('#av-last-sub').textContent = fmtDate(scan.finished_at) + ' · ' + (scan.path || '');
        body.querySelector('#av-last-badge').innerHTML = clean
            ? `<span class="pwr-badge pwr-badge-green"><i class="fas fa-check"></i> ${t('Czyste')}</span>`
            : `<span class="pwr-badge av-badge-red"><i class="fas fa-bug"></i> ${t('Zagrożenia')}: ${scan.infected_count}</span>`;

        const infoHtml = `
            <div class="pwr-info-row" style="background:none;border-top:1px solid var(--border)">
                <span style="color:var(--text-muted)">${t('Pliki')}</span>
                <span>${scan.scanned_files}</span>
            </div>
            <div class="pwr-info-row" style="background:none">
                <span style="color:var(--text-muted)">${t('Zagrożenia')}</span>
                <span style="color:${scan.infected_count > 0 ? '#ef4444' : 'var(--text-muted)'}">${scan.infected_count}</span>
            </div>
            <div class="pwr-info-row" style="background:none">
                <span style="color:var(--text-muted)">${t('Czas')}</span>
                <span>${fmtDuration(scan.scan_time)}</span>
            </div>
            ${scan.infected_files && scan.infected_files.length > 0 ? `
            <div style="margin-top:6px;background:rgba(239,68,68,.06);border:1px solid rgba(239,68,68,.2);border-radius:6px;padding:6px 10px">
                ${scan.infected_files.map(f => `<div class="av-threat-item"><i class="fas fa-bug" style="color:#ef4444;font-size:10px"></i> <span style="font-family:monospace;font-size:11px">${esc(f)}</span></div>`).join('')}
            </div>` : ''}`;
        body.querySelector('#av-last-body').innerHTML = infoHtml;
    }

    // ══════════════════════════════════════════════════════════
    // UPDATE DB
    // ══════════════════════════════════════════════════════════

    async function startUpdateDb() {
        const wrap = body.querySelector('#av-updatedb-wrap');
        const fill = body.querySelector('#av-updatedb-fill');
        const msg  = body.querySelector('#av-updatedb-msg');
        const btn  = body.querySelector('#av-update-db-btn');
        wrap.style.display = '';
        btn.disabled = true;

        const r = await api('/antivirus/update-db', { method: 'POST' });
        if (r.error) { toast(r.error, 'error'); btn.disabled = false; return; }

        const handler = (ev) => {
            if (ev.task_id !== r.task_id) return;
            if (ev.percent >= 0) fill.style.width = ev.percent + '%';
            msg.textContent = ev.message || '';
            if (ev.stage === 'done') {
                fill.style.width = '100%';
                if (NAS.socket) NAS.socket.off('antivirus_update_db', handler);
                btn.disabled = false;
                toast(t('Baza wirusów zaktualizowana!'), 'success');
                setTimeout(() => { wrap.style.display = 'none'; fill.style.width = '0'; }, 1500);
            }
            if (ev.stage === 'error') {
                toast(ev.message, 'error');
                if (NAS.socket) NAS.socket.off('antivirus_update_db', handler);
                wrap.style.display = 'none';
                btn.disabled = false;
            }
        };
        if (NAS.socket) NAS.socket.on('antivirus_update_db', handler);
    }

    // ══════════════════════════════════════════════════════════
    // INSTALL
    // ══════════════════════════════════════════════════════════

    async function startInstall() {
        const btn  = body.querySelector('#av-install-btn');
        const wrap = body.querySelector('#av-install-wrap');
        const fill = body.querySelector('#av-install-fill');
        const msg  = body.querySelector('#av-install-msg');
        btn.disabled = true;
        wrap.style.display = '';

        const r = await api('/antivirus/install', { method: 'POST' });
        if (r.error) { toast(r.error, 'error'); btn.disabled = false; return; }

        const handler = (ev) => {
            if (ev.task_id !== r.task_id) return;
            if (ev.percent >= 0) fill.style.width = ev.percent + '%';
            msg.textContent = ev.message || '';
            if (ev.stage === 'done') {
                if (NAS.socket) NAS.socket.off('antivirus_install', handler);
                // Reload entire app view
                body.querySelector('#av-install-card').style.display = 'none';
                loadAll();
            }
            if (ev.stage === 'error') {
                toast(ev.message, 'error');
                if (NAS.socket) NAS.socket.off('antivirus_install', handler);
                btn.disabled = false;
            }
        };
        if (NAS.socket) NAS.socket.on('antivirus_install', handler);
    }

    // ══════════════════════════════════════════════════════════
    // SCHEDULES
    // ══════════════════════════════════════════════════════════

    async function loadSchedules() {
        const list = body.querySelector('#av-sched-list');
        const data = await api('/antivirus/schedules');
        const items = data.items || [];

        if (items.length === 0) {
            list.innerHTML = `<div class="pwr-empty"><i class="fas fa-clock"></i> ${t('Brak zaplanowanych skanów.')}</div>`;
            return;
        }

        list.innerHTML = items.map(s => {
            const log   = s.last_log;
            const clean = log ? log.infected_count === 0 : null;
            const badge = log !== null ? (clean
                ? `<span class="pwr-badge pwr-badge-green"><i class="fas fa-check"></i> ${t('Czyste')}</span>`
                : `<span class="pwr-badge av-badge-red"><i class="fas fa-bug"></i> ${log.infected_count}</span>`) : '';
            return `
            <div class="pwr-info-row" style="flex-wrap:wrap;gap:8px;background:none;border-top:1px solid var(--border)">
                <div style="flex:1;min-width:0">
                    <div style="font-size:13px;font-weight:600;color:var(--text-primary)">${esc(s.name)}</div>
                    <div style="font-size:11px;color:var(--text-muted);margin-top:2px">
                        <i class="fas fa-clock"></i> ${esc(cronLabel(s.cron_expr))} &nbsp;
                        <i class="fas fa-folder"></i> ${esc(s.path)}
                        ${log ? ` &nbsp; <i class="fas fa-calendar"></i> ${fmtDate(log.finished_at)}` : ''}
                    </div>
                </div>
                <div style="display:flex;align-items:center;gap:6px;flex-shrink:0">
                    ${badge}
                    <label class="pwr-toggle pwr-toggle-sm" title="${t('Włącz / wyłącz')}">
                        <input type="checkbox" class="av-sched-toggle" data-sid="${esc(s.id)}" ${s.enabled ? 'checked' : ''}>
                        <span class="pwr-toggle-track"><span class="pwr-toggle-thumb"></span></span>
                    </label>
                    <button class="pwr-icon-btn av-sched-edit" data-sid="${esc(s.id)}" title="${t('Edytuj')}">
                        <i class="fas fa-pencil"></i>
                    </button>
                    <button class="pwr-icon-btn av-sched-delete" data-sid="${esc(s.id)}" data-name="${esc(s.name)}" title="${t('Usuń')}" style="color:var(--danger,#ef4444)">
                        <i class="fas fa-trash"></i>
                    </button>
                </div>
            </div>`;
        }).join('');

        list.querySelectorAll('.av-sched-toggle').forEach(el => {
            el.addEventListener('change', () => {
                api('/antivirus/schedules/' + el.dataset.sid, { method: 'PUT', body: { enabled: el.checked } })
                    .then(r => { if (r.error) { toast(r.error, 'error'); loadSchedules(); } });
            });
        });
        list.querySelectorAll('.av-sched-edit').forEach(el => {
            el.addEventListener('click', () => openSchedModal(el.dataset.sid));
        });
        list.querySelectorAll('.av-sched-delete').forEach(el => {
            el.addEventListener('click', async () => {
                if (!await confirmDialog(t('Usunąć harmonogram') + ' "' + el.dataset.name + '"?')) return;
                const r = await api('/antivirus/schedules/' + el.dataset.sid, { method: 'DELETE' });
                if (r.error) toast(r.error, 'error');
                else loadSchedules();
            });
        });
    }

    // ══════════════════════════════════════════════════════════
    // HISTORY
    // ══════════════════════════════════════════════════════════

    async function loadHistory() {
        const list  = body.querySelector('#av-history-list');
        const data  = await api('/antivirus/results');
        const items = data.items || [];

        if (items.length === 0) {
            list.innerHTML = `<div class="pwr-empty"><i class="fas fa-history"></i> ${t('Brak historii skanowań.')}</div>`;
            return;
        }

        list.innerHTML = items.map(item => {
            const clean = item.status === 'clean';
            return `
            <div class="pwr-info-row" style="flex-wrap:wrap;background:none;border-top:1px solid var(--border);gap:8px">
                <div style="flex:1;min-width:0">
                    <div style="display:flex;align-items:center;gap:6px;flex-wrap:wrap">
                        ${clean
                            ? `<span class="pwr-badge pwr-badge-green"><i class="fas fa-check"></i> ${t('Czyste')}</span>`
                            : `<span class="pwr-badge av-badge-red"><i class="fas fa-bug"></i> ${item.infected_count}</span>`}
                        <span style="font-size:12px;color:var(--text-muted);font-family:monospace">${esc(item.path)}</span>
                    </div>
                    <div style="font-size:11px;color:var(--text-muted);margin-top:3px">
                        ${fmtDate(item.finished_at)} &nbsp;·&nbsp;
                        <i class="fas fa-file"></i> ${item.scanned_files} &nbsp;·&nbsp;
                        <i class="fas fa-stopwatch"></i> ${fmtDuration(item.scan_time)}
                        &nbsp;·&nbsp; <span style="background:var(--overlay-1);border-radius:10px;padding:1px 7px;font-size:10px">${item.type === 'manual' ? t('Ręczne') : t('Zaplanowane')}</span>
                    </div>
                    ${item.infected_files && item.infected_files.length > 0 ? `
                    <div style="margin-top:4px;background:rgba(239,68,68,.06);border:1px solid rgba(239,68,68,.2);border-radius:4px;padding:4px 8px">
                        ${item.infected_files.slice(0,5).map(f => `<div class="av-threat-item"><i class="fas fa-bug" style="color:#ef4444;font-size:10px"></i> <span style="font-family:monospace;font-size:11px">${esc(f)}</span></div>`).join('')}
                    </div>` : ''}
                </div>
                <button class="pwr-icon-btn av-hist-delete" data-id="${esc(item.id)}" title="${t('Usuń')}" style="color:var(--danger,#ef4444);flex-shrink:0">
                    <i class="fas fa-trash"></i>
                </button>
            </div>`;
        }).join('');

        list.querySelectorAll('.av-hist-delete').forEach(btn => {
            btn.addEventListener('click', async () => {
                const r = await api('/antivirus/results/' + btn.dataset.id, { method: 'DELETE' });
                if (r.error) toast(r.error, 'error');
                else loadHistory();
            });
        });
    }

    // ══════════════════════════════════════════════════════════
    // SCHEDULE MODAL
    // ══════════════════════════════════════════════════════════

    let _editSid = null;

    function wireModal() {
        const preset    = body.querySelector('#av-f-preset');
        const customRow = body.querySelector('#av-f-custom-row');

        preset.addEventListener('change', () => {
            customRow.style.display = preset.value === 'custom' ? '' : 'none';
        });

        body.querySelector('#av-f-browse').addEventListener('click', () => {
            openDirPicker(body.querySelector('#av-f-path').value || '/home', t('Wybierz folder'), (p) => {
                body.querySelector('#av-f-path').value = p;
            });
        });

        body.querySelector('#av-dlg-x').addEventListener('click', closeSchedModal);
        body.querySelector('#av-dlg-cancel').addEventListener('click', closeSchedModal);
        scanOverlay.addEventListener('click', e => { if (e.target === scanOverlay) closeSchedModal(); });

        body.querySelector('#av-dlg-save').addEventListener('click', async () => {
            const name    = body.querySelector('#av-f-name').value.trim();
            const path    = body.querySelector('#av-f-path').value.trim();
            const preset  = body.querySelector('#av-f-preset').value;
            const cron    = preset === 'custom'
                ? body.querySelector('#av-f-cron').value.trim()
                : preset;
            const enabled = body.querySelector('#av-f-enabled').checked;

            if (!name) { toast(t('Podaj nazwę'), 'error'); return; }

            const endpoint = _editSid ? '/antivirus/schedules/' + _editSid : '/antivirus/schedules';
            const method   = _editSid ? 'PUT' : 'POST';
            const r = await api(endpoint, { method, body: { name, path, cron_expr: cron, enabled } });
            if (r.error) { toast(r.error, 'error'); return; }
            closeSchedModal();
            loadSchedules();
        });
    }

    function openSchedModal(sid) {
        _editSid = sid || null;
        body.querySelector('#av-dlg-title').textContent = sid ? t('Edytuj harmonogram') : t('Nowy harmonogram');
        body.querySelector('#av-f-name').value    = '';
        body.querySelector('#av-f-path').value    = '/home';
        body.querySelector('#av-f-preset').value  = '0 2 * * 0';
        body.querySelector('#av-f-cron').value    = '';
        body.querySelector('#av-f-enabled').checked = true;
        body.querySelector('#av-f-custom-row').style.display = 'none';

        if (sid) {
            api('/antivirus/schedules').then(d => {
                const s = (d.items || []).find(x => x.id === sid);
                if (!s) return;
                body.querySelector('#av-f-name').value   = s.name;
                body.querySelector('#av-f-path').value   = s.path;
                body.querySelector('#av-f-enabled').checked = s.enabled;
                const known = ['0 * * * *','0 2 * * *','0 2 * * 0','0 2 1 * *'].includes(s.cron_expr);
                if (known) {
                    body.querySelector('#av-f-preset').value = s.cron_expr;
                } else {
                    body.querySelector('#av-f-preset').value = 'custom';
                    body.querySelector('#av-f-cron').value   = s.cron_expr;
                    body.querySelector('#av-f-custom-row').style.display = '';
                }
            });
        }

        scanOverlay.classList.add('visible');
        setTimeout(() => body.querySelector('#av-f-name').focus(), 80);
    }

    function closeSchedModal() {
        scanOverlay.classList.remove('visible');
        _editSid = null;
    }

    // ── Wire static event listeners (once) ─────────────────────
    body.querySelector('#av-install-btn').addEventListener('click', startInstall);
    body.querySelector('#av-update-db-btn').addEventListener('click', startUpdateDb);
    body.querySelector('#av-browse-btn').addEventListener('click', () => {
        const pathInput = body.querySelector('#av-scan-path');
        openDirPicker(pathInput.value || '/home', t('Wybierz folder do skanowania'), (p) => {
            pathInput.value = p;
        });
    });
    body.querySelector('#av-scan-btn').addEventListener('click', () => {
        const path = body.querySelector('#av-scan-path').value.trim() || '/home';
        startScan(path);
    });
    body.querySelector('#av-cancel-btn').addEventListener('click', async () => {
        await api('/antivirus/scan/cancel', { method: 'POST' });
        body.querySelector('#av-cancel-btn').style.display = 'none';
        const scanBtn = body.querySelector('#av-scan-btn');
        scanBtn.disabled = false;
        scanBtn.innerHTML = `<i class="fas fa-play"></i> ${t('Skanuj')}`;
        body.querySelector('#av-scan-path').disabled = false;
        body.querySelector('#av-scan-progress').style.display = 'none';
        body.querySelector('#av-prog-stats').textContent = '';
        scanningChip.style.display = 'none';
    });
    body.querySelector('#av-add-sched-btn').addEventListener('click', () => openSchedModal());
    body.querySelector('#av-uninstall-btn').addEventListener('click', async () => {
        if (!await confirmDialog(t('Czy na pewno chcesz odinstalować ClamAV?'))) return;
        const btn = body.querySelector('#av-uninstall-btn');
        btn.disabled = true;
        const r = await api('/antivirus/uninstall', { method: 'POST' });
        btn.disabled = false;
        if (r.error) { toast(r.error, 'error'); return; }
        toast(t('ClamAV odinstalowany'), 'success');
        ['#av-engine-card','#av-scan-card','#av-last-scan-card','#av-sched-card','#av-history-card']
            .forEach(sel => { body.querySelector(sel).style.display = 'none'; });
        dbChip.style.display = 'none';
        scanningChip.style.display = 'none';
        loadAll();
    });
    wireModal();

    // ── Init ──────────────────────────────────────────────────
    loadAll();
};
