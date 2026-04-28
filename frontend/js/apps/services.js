/* ═══════════════════════════════════════════════════════════
   ${t('EthOS — Usługi (Service Manager)')}
   Start / Stop / Restart / Enable / Disable / Uninstall / Logs
   ═══════════════════════════════════════════════════════════ */

AppRegistry['services'] = function (appDef) {
    const _cl = (level, msg, details) => typeof NAS !== 'undefined' && NAS.logClient
        ? NAS.logClient('services', level, msg, details) : console.log('[services]', msg, details || '');

    createWindow('services', {
        title: t('Usługi'),
        icon: appDef.icon,
        iconColor: appDef.color,
        width: 820,
        height: 560,
        onRender: (body) => renderServicesApp(body),
    });
};

function renderServicesApp(body) {
    const $ = (s) => body.querySelector(s);
    let allServices = [];
    let filter = 'all';    // all | known | running | stopped
    let search = '';

    body.innerHTML = `
    <div class="svc-app" style="display:flex;flex-direction:column;height:100%;overflow:hidden">
        <div style="display:flex;align-items:center;gap:8px;padding:10px 14px;border-bottom:1px solid var(--border);flex-wrap:wrap;flex-shrink:0">
            <span style="font-weight:600;font-size:14px"><i class="fas fa-cogs" style="margin-right:6px;color:#64748b"></i>${t('Usługi systemowe')}</span>
            <div style="flex:1"></div>
            <div style="display:flex;gap:2px;background:var(--bg-secondary);border-radius:6px;padding:2px">
                <button class="svc-filt" data-f="all" style="padding:4px 10px;font-size:12px;border:none;border-radius:4px;cursor:pointer;background:var(--bg-primary);font-weight:600">Wszystkie</button>
                <button class="svc-filt" data-f="installed" style="padding:4px 10px;font-size:12px;border:none;border-radius:4px;cursor:pointer;background:none">Zainstalowane</button>
                <button class="svc-filt" data-f="running" style="padding:4px 10px;font-size:12px;border:none;border-radius:4px;cursor:pointer;background:none">Aktywne</button>
                <button class="svc-filt" data-f="available" style="padding:4px 10px;font-size:12px;border:none;border-radius:4px;cursor:pointer;background:none">${t('Dostępne')}</button>
            </div>
            <input type="text" id="svc-search" class="fm-input" placeholder="${t('Szukaj…')}" style="width:160px;font-size:12px">
            <button class="fm-toolbar-btn" id="svc-refresh" title="${t('Odśwież')}"><i class="fas fa-sync-alt"></i></button>
        </div>
        <div id="svc-list" style="flex:1;overflow-y:auto;padding:4px 0"></div>
        <div id="svc-install-progress" style="display:none;border-top:1px solid var(--border);flex-shrink:0;background:var(--bg-secondary)">
            <div style="padding:12px 14px;">
                <div style="display:flex;align-items:center;gap:10px;margin-bottom:8px;">
                    <i id="svc-inst-icon" class="fas fa-spinner fa-spin" style="font-size:16px;color:var(--accent)"></i>
                    <div style="flex:1">
                        <div style="font-size:13px;font-weight:700;color:var(--text)" id="svc-inst-title">Instalowanie…</div>
                        <div style="font-size:11px;color:var(--text-secondary);margin-top:2px;white-space:nowrap;overflow:hidden;text-overflow:ellipsis;max-width:500px" id="svc-inst-msg"></div>
                    </div>
                    <div style="font-size:18px;font-weight:700;color:var(--accent);min-width:42px;text-align:right" id="svc-inst-pct">0%</div>
                </div>
                <div style="background:var(--bg-primary);border-radius:5px;height:6px;overflow:hidden">
                    <div id="svc-inst-fill" style="height:100%;background:linear-gradient(90deg,#6366f1,#8b5cf6);width:0%;transition:width .3s ease;border-radius:5px"></div>
                </div>
                <div style="display:flex;justify-content:space-between;margin-top:6px;font-size:10px;color:var(--text-secondary)">
                    <span data-phase="update" class="svc-inst-step">● Aktualizacja</span>
                    <span data-phase="download" class="svc-inst-step">● Pobieranie</span>
                    <span data-phase="unpack" class="svc-inst-step">● Rozpakowywanie</span>
                    <span data-phase="configure" class="svc-inst-step">● Konfiguracja</span>
                    <span data-phase="enable" class="svc-inst-step">● Uruchamianie</span>
                </div>
            </div>
        </div>
        <div id="svc-logs-panel" style="display:none;border-top:1px solid var(--border);max-height:200px;overflow-y:auto;flex-shrink:0"></div>
    </div>`;

    // Filter buttons
    body.querySelectorAll('.svc-filt').forEach(b => {
        b.onclick = () => {
            filter = b.dataset.f;
            body.querySelectorAll('.svc-filt').forEach(x => {
                x.style.background = x.dataset.f === filter ? 'var(--bg-primary)' : 'none';
                x.style.fontWeight = x.dataset.f === filter ? '600' : '400';
            });
            renderList();
        };
    });

    $('#svc-search').oninput = (e) => { search = e.target.value.toLowerCase(); renderList(); };
    $('#svc-refresh').onclick = () => loadServices();

    async function loadServices() {
        const list = $('#svc-list');
        list.innerHTML = `<div style="padding:30px;text-align:center;color:var(--text-muted)"><i class="fas fa-spinner fa-spin"></i> ${t('Ładowanie…')}</div>`;
        try {
            const data = await api('/services/list');
            allServices = data.services || [];
            renderList();
        } catch (e) {
            list.innerHTML = `<div style="padding:20px;color:#ef4444">${t('Błąd:')} ${e.message}</div>`;
        }
    }

    function renderList() {
        const list = $('#svc-list');
        let svcs = allServices;

        // Apply filters
        if (filter === 'installed') svcs = svcs.filter(s => s.installed !== false);
        else if (filter === 'running') svcs = svcs.filter(s => s.active);
        else if (filter === 'available') svcs = svcs.filter(s => s.installed === false);

        if (search) svcs = svcs.filter(s => s.name.toLowerCase().includes(search) || s.id.toLowerCase().includes(search));

        if (!svcs.length) {
            list.innerHTML = `<div style="padding:30px;text-align:center;color:var(--text-muted)">${t('Brak usług')}</div>`;
            return;
        }

        // Group by category
        const groups = {};
        svcs.forEach(s => {
            const cat = s.category || 'Inne';
            if (!groups[cat]) groups[cat] = [];
            groups[cat].push(s);
        });

        let html = '';
        const catOrder = ['System', 'Sharing', 'Apps', 'Inne'];
        const sortedCats = Object.keys(groups).sort((a, b) => {
            const ai = catOrder.indexOf(a), bi = catOrder.indexOf(b);
            return (ai === -1 ? 99 : ai) - (bi === -1 ? 99 : bi);
        });

        for (const cat of sortedCats) {
            html += `<div style="padding:6px 14px;font-size:11px;font-weight:600;color:var(--text-muted);text-transform:uppercase;letter-spacing:.5px;background:var(--bg-secondary)">${cat}</div>`;
            for (const s of groups[cat]) {
                const notInstalled = s.installed === false;
                const stateColor = notInstalled ? 'var(--text-muted)' : (s.active ? '#10b981' : (s.state === 'failed' ? '#ef4444' : 'var(--text-muted)'));
                const stateLabel = notInstalled ? 'Nie zainstalowany' : (s.active ? (s.state === 'running' ? t('Działa') : 'Aktywna') : (s.state === 'failed' ? t('Błąd') : 'Zatrzymana'));
                const enabledLabel = notInstalled ? '' : (s.masked ? 'Zablokowana' : (s.enabled === 'enabled' ? 'Autostart' : (s.enabled === 'disabled' ? t('Ręczna') : (s.enabled === 'static' ? 'Statyczna' : ''))));
                const enabledColor = s.masked ? '#ef4444' : (s.enabled === 'enabled' ? '#10b981' : 'var(--text-muted)');

                let actions = '';
                if (notInstalled) {
                    actions = `<button class="fm-toolbar-btn btn-sm btn-green svc-install" data-svc="${s.id}" data-pkg="${s.pkg}"><i class="fas fa-download"></i> Zainstaluj</button>`;
                } else {
                    if (s.active) {
                        actions += `<button class="fm-toolbar-btn btn-sm svc-act" data-svc="${s.id}" data-action="restart" title="Restart"><i class="fas fa-redo"></i></button>`;
                        actions += `<button class="fm-toolbar-btn btn-sm btn-red svc-act" data-svc="${s.id}" data-action="stop" title="Zatrzymaj"><i class="fas fa-stop"></i></button>`;
                    } else {
                        actions += `<button class="fm-toolbar-btn btn-sm btn-green svc-act" data-svc="${s.id}" data-action="start" title="Uruchom"><i class="fas fa-play"></i></button>`;
                    }
                    if (s.enabled === 'enabled') {
                        actions += `<button class="fm-toolbar-btn btn-sm svc-act" data-svc="${s.id}" data-action="disable" title="${t('Wyłącz autostart')}"><i class="fas fa-toggle-on" style="color:#10b981"></i></button>`;
                    } else if (s.enabled === 'disabled') {
                        actions += `<button class="fm-toolbar-btn btn-sm svc-act" data-svc="${s.id}" data-action="enable" title="${t('Włącz autostart')}"><i class="fas fa-toggle-off"></i></button>`;
                    }
                    actions += `<button class="fm-toolbar-btn btn-sm svc-log" data-svc="${s.id}" title="Logi"><i class="fas fa-rectangle-list"></i></button>`;
                    if (s.pkg) {
                        actions += `<button class="fm-toolbar-btn btn-sm btn-red svc-act" data-svc="${s.id}" data-action="uninstall" data-pkg="${s.pkg}" title="Odinstaluj"><i class="fas fa-trash"></i></button>`;
                    }
                }

                html += `<div class="svc-row" data-id="${s.id}" style="display:flex;align-items:center;gap:10px;padding:8px 14px;border-bottom:1px solid var(--border);transition:background .1s;cursor:default${notInstalled ? ';opacity:.6' : ''}">
                    <i class="fas ${s.icon}" style="width:20px;text-align:center;color:${stateColor};font-size:13px"></i>
                    <div style="flex:1;min-width:0">
                        <div style="font-size:13px;font-weight:500;white-space:nowrap;overflow:hidden;text-overflow:ellipsis">${s.name}</div>
                        <div style="font-size:11px;color:var(--text-muted)">${s.id}.service</div>
                    </div>
                    <span style="font-size:11px;color:${stateColor};font-weight:500;min-width:80px">${stateLabel}</span>
                    <span style="font-size:11px;color:${enabledColor};min-width:70px">${enabledLabel}</span>
                    <div class="svc-actions" style="display:flex;gap:3px">${actions}</div>
                </div>`;
            }
        }

        list.innerHTML = html;

        // Hover effect
        list.querySelectorAll('.svc-row').forEach(r => {
            r.onmouseenter = () => r.style.background = 'var(--bg-hover)';
            r.onmouseleave = () => r.style.background = '';
        });

        // Action buttons
        list.querySelectorAll('.svc-act').forEach(btn => {
            btn.onclick = async (e) => {
                e.stopPropagation();
                const svc = btn.dataset.svc;
                const action = btn.dataset.action;

                if (action === 'stop' && !await confirmDialog(t('Zatrzymać') + ' ' + svc + '?')) return;
                if (action === 'uninstall' && !await confirmDialog(t('Odinstalować') + ' ' + svc + ' (' + btn.dataset.pkg + ')? ' + t('To usunie pakiet z systemu.'))) return;

                btn.disabled = true;
                const origIcon = btn.innerHTML;
                btn.innerHTML = '<i class="fas fa-spinner fa-spin"></i>';

                try {
                    const res = await api('/services/action', { method: 'POST', body: { service: svc, action } });
                    toast(res.message || `${svc}: ${action}`, 'success');
                    // Reload list after short delay
                    setTimeout(() => loadServices(), 500);
                } catch (e) {
                    toast(e.message || t('Błąd'), 'error');
                    btn.innerHTML = origIcon;
                    btn.disabled = false;
                }
            };
        });

        // Log buttons
        list.querySelectorAll('.svc-log').forEach(btn => {
            btn.onclick = async (e) => {
                e.stopPropagation();
                showLogs(btn.dataset.svc);
            };
        });

        // Install buttons
        list.querySelectorAll('.svc-install').forEach(btn => {
            btn.onclick = async (e) => {
                e.stopPropagation();
                const svc = btn.dataset.svc;
                const pkg = btn.dataset.pkg;
                if (!pkg) return;
                btn.disabled = true;
                btn.innerHTML = '<i class="fas fa-spinner fa-spin"></i> Instalowanie…';
                // Show progress panel
                const prog = $('#svc-install-progress');
                if (prog) {
                    prog.style.display = '';
                    $('#svc-inst-title').textContent = `Instalowanie ${pkg}…`;
                    $('#svc-inst-msg').textContent = 'Przygotowywanie…';
                    $('#svc-inst-pct').textContent = '0%';
                    $('#svc-inst-fill').style.width = '0%';
                    const icon = $('#svc-inst-icon');
                    icon.className = 'fas fa-spinner fa-spin';
                    icon.style.color = 'var(--accent)';
                    body.querySelectorAll('.svc-inst-step').forEach(s => {
                        s.style.color = 'var(--text-secondary)';
                        s.style.fontWeight = '400';
                    });
                }
                try {
                    const res = await api('/services/action', { method: 'POST', body: { service: svc, action: 'install' } });
                    if (res.error) {
                        toast(res.error, 'error');
                        btn.innerHTML = '<i class="fas fa-download"></i> Zainstaluj';
                        btn.disabled = false;
                        if (prog) prog.style.display = 'none';
                    }
                    // If async, progress will come via SocketIO
                } catch (e) {
                    toast(e.message || t('Błąd instalacji'), 'error');
                    btn.innerHTML = '<i class="fas fa-download"></i> Zainstaluj';
                    btn.disabled = false;
                    if (prog) prog.style.display = 'none';
                }
            };
        });
    }

    async function showLogs(svc) {
        const panel = $('#svc-logs-panel');
        panel.style.display = '';
        panel.innerHTML = `<div style="padding:8px 14px;display:flex;align-items:center;gap:8px;border-bottom:1px solid var(--border);background:var(--bg-secondary)">
            <i class="fas fa-rectangle-list" style="color:#64748b"></i>
            <span style="font-size:13px;font-weight:500">${svc}</span>
            <div style="flex:1"></div>
            <button class="fm-toolbar-btn btn-sm" id="svc-logs-more" title="${t('Więcej')}"><i class="fas fa-plus"></i> ${t('200 linii')}</button>
            <button class="fm-toolbar-btn btn-sm" id="svc-logs-close"><i class="fas fa-times"></i></button>
        </div>
        <pre id="svc-logs-content" style="padding:8px 14px;font-size:11px;font-family:monospace;margin:0;white-space:pre-wrap;word-break:break-all;max-height:150px;overflow-y:auto;color:var(--text-muted)"><i class="fas fa-spinner fa-spin"></i></pre>`;

        panel.querySelector('#svc-logs-close').onclick = () => { panel.style.display = 'none'; };

        let lines = 50;
        async function fetchLogs() {
            try {
                const data = await api(`/services/logs?service=${encodeURIComponent(svc)}&lines=${lines}`);
                panel.querySelector('#svc-logs-content').textContent = data.logs || t('(brak logów)');
                // Scroll to bottom
                const pre = panel.querySelector('#svc-logs-content');
                pre.scrollTop = pre.scrollHeight;
            } catch (e) {
                panel.querySelector('#svc-logs-content').textContent = t('Błąd: ') + e.message;
            }
        }

        panel.querySelector('#svc-logs-more').onclick = () => { lines = 200; fetchLogs(); };
        fetchLogs();
    }

    loadServices();

    // SocketIO: live install progress
    if (typeof socket !== 'undefined') {
        const _onInstallProgress = (data) => {
            const prog = $('#svc-install-progress');
            if (!prog) return;

            const pct = data.progress || 0;
            const phase = data.phase || 'install';
            const msg = data.message || '';

            if (phase === 'done') {
                // Success
                prog.style.display = '';
                $('#svc-inst-title').textContent = data.message || 'Zainstalowano';
                $('#svc-inst-msg').textContent = '';
                $('#svc-inst-pct').textContent = '100%';
                $('#svc-inst-fill').style.width = '100%';
                $('#svc-inst-fill').style.background = 'linear-gradient(90deg,#22c55e,#16a34a)';
                const icon = $('#svc-inst-icon');
                icon.className = 'fas fa-check-circle';
                icon.style.color = '#22c55e';
                body.querySelectorAll('.svc-inst-step').forEach(s => {
                    s.style.color = '#22c55e'; s.style.fontWeight = '600';
                });
                toast(data.message || `${data.pkg} zainstalowany`, 'success');
                setTimeout(() => {
                    prog.style.display = 'none';
                    $('#svc-inst-fill').style.background = 'linear-gradient(90deg,#6366f1,#8b5cf6)';
                    loadServices();
                }, 2500);
                return;
            }

            if (phase === 'error') {
                // Error
                prog.style.display = '';
                $('#svc-inst-title').textContent = data.message || t('Błąd instalacji');
                $('#svc-inst-msg').textContent = data.detail || '';
                $('#svc-inst-pct').textContent = '✕';
                $('#svc-inst-pct').style.color = '#ef4444';
                $('#svc-inst-fill').style.width = '100%';
                $('#svc-inst-fill').style.background = '#ef4444';
                const icon = $('#svc-inst-icon');
                icon.className = 'fas fa-times-circle';
                icon.style.color = '#ef4444';
                toast(data.message || t('Błąd instalacji'), 'error');
                setTimeout(() => {
                    prog.style.display = 'none';
                    $('#svc-inst-fill').style.background = 'linear-gradient(90deg,#6366f1,#8b5cf6)';
                    $('#svc-inst-pct').style.color = 'var(--accent)';
                    loadServices();
                }, 4000);
                return;
            }

            // In progress
            prog.style.display = '';
            $('#svc-inst-title').textContent = `Instalowanie ${data.pkg || ''}…`;
            $('#svc-inst-msg').textContent = msg;
            $('#svc-inst-pct').textContent = pct + '%';
            $('#svc-inst-fill').style.width = pct + '%';

            // Highlight completed phase dots
            const phaseOrder = ['update', 'download', 'unpack', 'configure', 'enable'];
            const currentIdx = phaseOrder.indexOf(phase);
            body.querySelectorAll('.svc-inst-step').forEach(s => {
                const idx = phaseOrder.indexOf(s.dataset.phase);
                if (idx >= 0 && idx <= currentIdx) {
                    s.style.color = 'var(--accent)';
                    s.style.fontWeight = '600';
                } else {
                    s.style.color = 'var(--text-secondary)';
                    s.style.fontWeight = '400';
                }
            });
        };

        socket.on('service_install_progress', _onInstallProgress);

        // Cleanup on window close — store reference for potential removal
        body._svcInstallCleanup = () => {
            socket.off('service_install_progress', _onInstallProgress);
        };
    }
}
