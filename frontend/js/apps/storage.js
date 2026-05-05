/* ═══════════════════════════════════════════════════════════
   EthOS — Unified Storage Manager (Synology-style)
   Disks · RAID · Volumes · Sharing · Diagnostics · SSD Cache
   ═══════════════════════════════════════════════════════════ */

/* ── Sharing helpers (module-level) ──────────────────────── */
function _shBadge(ok, label) {
    if (ok === null) return `<span class="fm-badge shr-badge-loading">${label || '…'}</span>`;
    if (ok === 'off') return `<span class="fm-badge shr-badge-warn">${label || t('Wyłączony')}</span>`;
    return ok
        ? `<span class="fm-badge fm-badge-green">${label || t('Aktywny')}</span>`
        : `<span class="fm-badge shr-badge-err">${label || t('Nieaktywny')}</span>`;
}

function _shEsc(s) {
    const d = document.createElement('div');
    d.textContent = s;
    return d.innerHTML;
}

function _shEscAttr(s) {
    return String(s).replace(/&/g,'&amp;').replace(/"/g,'&quot;').replace(/'/g,'&#39;').replace(/</g,'&lt;').replace(/>/g,'&gt;');
}

/* All 6 protocols — order matters for sidebar */
const _SH_ALL_PROTOS = [
    { id: 'samba',  pkgId: 'sharing-samba',  icon: 'fa-windows',      label: 'Samba' },
    { id: 'nfs',    pkgId: 'sharing-nfs',    icon: 'fa-network-wired', label: 'NFS' },
    { id: 'dlna',   pkgId: 'sharing-dlna',   icon: 'fa-photo-video',  label: 'DLNA' },
    { id: 'webdav', pkgId: 'sharing-webdav', icon: 'fa-globe',        label: 'WebDAV' },
    { id: 'sftp',   pkgId: 'sharing-sftp',   icon: 'fa-lock',         label: 'SFTP' },
    { id: 'ftp',    pkgId: 'sharing-ftp',    icon: 'fa-upload',       label: 'FTP' },
];

/* ── main render ────────────────────────────── */

/* ── App Registration ────────────────────────────────────── */
AppRegistry['storage-manager'] = function (appDef, launchOpts) {
    const _cl = (level, msg, details) => typeof NAS !== 'undefined' && NAS.logClient
        ? NAS.logClient('storage-manager', level, msg, details) : console.log('[storage-manager]', msg, details || '');

    createWindow('storage-manager', {
        title: t('Menedżer dysków'),
        icon: appDef.icon,
        iconColor: appDef.color,
        width: 1200,
        height: 800,
        onRender: (body) => _smRender(body, launchOpts),
    });
};

function _smRender(body, launchOpts) {
    const sections = [
        { id: 'overview',     icon: 'fa-chart-pie',    label: t('Przegląd') },
        { id: 'pools',        icon: 'fa-layer-group',  label: t('Pule storage') },
        { id: 'wizard',       icon: 'fa-magic',        label: t('Kreator') },
        { id: 'disks',        icon: 'fa-hdd',          label: t('Dyski') },
        { id: 'raid',         icon: 'fa-server',       label: t('RAID / LVM') },
        { id: 'sharing',      icon: 'fa-share-alt',    label: t('Udostępnianie') },
        { id: 'diagnostics',  icon: 'fa-stethoscope',  label: t('Diagnostyka') },
        { id: 'maintenance', icon: 'fa-tools',        label: t('Konserwacja') },
        { id: 'cache',        icon: 'fa-bolt',         label: 'SSD Cache' },
    ];

    body.innerHTML = `
    <style>
    .sm-layout { display:flex; height:100%; overflow:hidden; }
    .sm-sidebar { width:210px; min-width:210px; background:var(--bg-secondary); border-right:1px solid var(--border-color); display:flex; flex-direction:column; overflow-y:auto; }
    .sm-sidebar-header { padding:16px 14px 12px; font-size:13px; font-weight:700; color:var(--text-secondary); text-transform:uppercase; letter-spacing:0.5px; }
    .sm-nav-item { display:flex; align-items:center; gap:10px; padding:10px 16px; cursor:pointer; color:var(--text-primary); font-size:13px; border-left:3px solid transparent; transition:background .15s; }
    .sm-nav-item:hover { background:var(--bg-hover); }
    .sm-nav-item.active { background:var(--bg-active, var(--bg-hover)); border-left-color:var(--accent); color:var(--accent); font-weight:600; }
    .sm-nav-item i { width:18px; text-align:center; font-size:14px; }
    .sm-content { flex:1; overflow-y:auto; min-width:0; }
    </style>
    <div class="sm-layout">
        <div class="sm-sidebar">
            <div class="sm-sidebar-header"><i class="fas fa-database" style="margin-right:6px"></i>${t('Storage Manager')}</div>
            ${sections.map(s => `<div class="sm-nav-item" data-section="${s.id}"><i class="fas ${s.icon}"></i><span>${s.label}</span></div>`).join('')}
        </div>
        <div class="sm-content" id="sm-content"></div>
    </div>`;

    const contentEl = body.querySelector('#sm-content');
    let activeSection = null;
    let cleanupFn = null;

    function switchSection(id) {
        if (activeSection === id) return;
        if (cleanupFn) { try { cleanupFn(); } catch(_){} cleanupFn = null; }
        activeSection = id;
        body.querySelectorAll('.sm-nav-item').forEach(n => n.classList.toggle('active', n.dataset.section === id));
        contentEl.innerHTML = '';
        contentEl.style.cssText = '';

        const setCleanup = (fn) => { if (activeSection === id) cleanupFn = fn; };

        switch (id) {
            case 'overview':     cleanupFn = _smOverview(contentEl, switchSection); break;
            case 'pools':        cleanupFn = _smPools(contentEl, switchSection); break;
            case 'wizard':       cleanupFn = _smWizard(contentEl, switchSection); break;
            case 'disks':        cleanupFn = _smDisks(contentEl); break;
            case 'raid':         cleanupFn = _smRaid(contentEl, 'arrays'); break;
            case 'sharing':      _smSharing(contentEl).then(fn => setCleanup(fn)); break;
            case 'diagnostics':  cleanupFn = _smDiagnostics(contentEl); break;
            case 'maintenance':  cleanupFn = _smMaintenance(contentEl); break;
            case 'cache':        cleanupFn = _smCache(contentEl); break;
        }
    }

    body.querySelectorAll('.sm-nav-item').forEach(n => n.onclick = () => switchSection(n.dataset.section));
    switchSection((launchOpts && launchOpts.section) || 'overview');
}

/* ═══════════════════════════════════════════════════════════
   Section: Overview (new dashboard)
   ═══════════════════════════════════════════════════════════ */
function _smOverview(el, switchSection) {
    el.innerHTML = '<div style="padding:20px" id="smo-root"><div style="color:var(--text-secondary)"><i class="fas fa-spinner fa-spin"></i> ' + t('Ładowanie...') + '</div></div>';
    const root = el.querySelector('#smo-root');

    async function load() {
        const [drivesRes, raidRes, drRes, poolsRes, healthRes] = await Promise.allSettled([
            api('/storage/drives'),
            api('/raid/arrays'),
            api('/diskrepair/disks'),
            api('/storage/pool/list'),
            api('/storage/health'),
        ]);

        const dVal = drivesRes.status === 'fulfilled' ? drivesRes.value : {};
        const drives = Array.isArray(dVal) ? dVal : (dVal.drives || []);
        const rVal = raidRes.status === 'fulfilled' ? raidRes.value : [];
        const arrays = Array.isArray(rVal) ? rVal : (rVal.arrays || []);
        const drVal = drRes.status === 'fulfilled' ? drRes.value : [];
        const drDisks = Array.isArray(drVal) ? drVal : (drVal.disks || []);
        const pVal = poolsRes.status === 'fulfilled' ? poolsRes.value : {};
        const pools = Array.isArray(pVal) ? pVal : (pVal.pools || []);
        const hVal = healthRes.status === 'fulfilled' ? healthRes.value : {};
        const healthAlerts = hVal.alerts || [];
        const lastCheck = hVal.last_check || null;

        const physDisks = drives.filter(d => d.type === 'disk' && !d.name.startsWith('nbd'));
        const parts = drives.filter(d => d.type === 'part' && d.usage);
        const totalBytes = parts.reduce((s, d) => s + (d.usage.total || 0), 0);
        const usedBytes = parts.reduce((s, d) => s + (d.usage.used || 0), 0);
        const healthyCount = drDisks.filter(d => d.smart_healthy === true).length;
        const smartAvail = drDisks.filter(d => d.smart_available).length;
        const warnCount = smartAvail - healthyCount;

        const fmt = (b) => {
            if (b >= 1e12) return (b/1e12).toFixed(1) + ' TB';
            if (b >= 1e9) return (b/1e9).toFixed(1) + ' GB';
            return (b/1e6).toFixed(0) + ' MB';
        };

        function card(icon, color, title, value, sub) {
            return `<div class="smo-card">
                <div class="smo-card-icon" style="color:${color}"><i class="fas ${icon}"></i></div>
                <div class="smo-card-value">${value}</div>
                <div class="smo-card-title">${title}</div>
                ${sub ? '<div class="smo-card-sub">' + sub + '</div>' : ''}
            </div>`;
        }

        let html = '<div class="smo-cards">';
        html += card('fa-layer-group', '#3b82f6', t('Pule storage'), pools.length,
            pools.length ? pools.filter(p => p.mounted).length + ' ' + t('aktywne') : t('Utwórz pierwszą pulę'));
        html += card('fa-hdd', 'var(--accent)', t('Dyski'), physDisks.length, `${fmt(totalBytes)} ${t('łącznie')}`);
        html += card('fa-heartbeat', warnCount > 0 ? '#e74c3c' : '#2ecc71', 'SMART', `${healthyCount}/${smartAvail}`,
            warnCount > 0 ? `${warnCount} ${t('ostrzeżeń')}` : t('Wszystko OK'));
        html += card('fa-chart-pie', '#9b59b6', t('Użyte'), totalBytes > 0 ? Math.round(usedBytes/totalBytes*100) + '%' : '—',
            `${fmt(usedBytes)} / ${fmt(totalBytes)}`);
        html += '</div>';

        // Health alerts panel
        if (healthAlerts.length > 0) {
            html += `<div class="smo-section" style="border-left:3px solid #e74c3c">
                <div class="smo-section-header">
                    <h4><i class="fas fa-exclamation-triangle" style="color:#e74c3c"></i> ${t('Alerty storage')}</h4>
                    <span style="font-size:11px;color:var(--text-muted)">${lastCheck ? t('Ostatnie sprawdzenie') + ': ' + new Date(lastCheck).toLocaleTimeString() : ''}</span>
                </div>`;
            for (const a of healthAlerts) {
                const icon = a.type === 'smart' ? 'fa-heartbeat' : a.type === 'raid' ? 'fa-server' : 'fa-chart-pie';
                const color = a.level === 'error' ? '#e74c3c' : '#f59e0b';
                html += `<div style="display:flex;align-items:center;gap:10px;padding:8px 12px;background:var(--bg-primary);border-radius:6px;margin-bottom:6px;font-size:13px">
                    <i class="fas ${icon}" style="color:${color};width:18px;text-align:center"></i>
                    <span style="flex:1">${a.message}</span>
                    <span class="smo-badge" style="background:${color}20;color:${color}">${a.level === 'error' ? t('Błąd') : t('Ostrzeżenie')}</span>
                </div>`;
            }
            html += '</div>';
        } else if (lastCheck) {
            html += `<div class="smo-section" style="border-left:3px solid #10b981">
                <div style="display:flex;align-items:center;gap:10px;padding:4px 0;font-size:13px">
                    <i class="fas fa-check-circle" style="color:#10b981;font-size:18px"></i>
                    <span style="font-weight:600;color:#10b981">${t('Brak problemów')}</span>
                    <span style="color:var(--text-muted);margin-left:auto;font-size:11px">${t('Ostatnie sprawdzenie')}: ${new Date(lastCheck).toLocaleTimeString()}</span>
                </div>
            </div>`;
        }
        if (pools.length) {
            html += `<div class="smo-section">
                <div class="smo-section-header">
                    <h4><i class="fas fa-layer-group"></i> ${t('Pule storage')}</h4>
                    <button class="smo-link-btn" id="smo-go-pools">${t('Zarządzaj')} <i class="fas fa-arrow-right"></i></button>
                </div>
                <div class="smo-pool-grid">`;

            for (const p of pools) {
                const u = p.usage || {};
                const pct = u.percent || 0;
                const barColor = pct > 90 ? '#e74c3c' : pct > 70 ? '#f59e0b' : '#10b981';
                const raidBadge = p.raid_level
                    ? `<span class="smo-badge smo-badge-blue">RAID ${p.raid_level}</span>`
                    : `<span class="smo-badge">${t('Pojedynczy')}</span>`;
                const statusDot = p.mounted
                    ? '<span class="smo-dot smo-dot-green"></span>'
                    : '<span class="smo-dot smo-dot-red"></span>';

                html += `<div class="smo-pool-card">
                    <div class="smo-pool-header">
                        ${statusDot}
                        <strong>${p.name}</strong>
                        ${raidBadge}
                    </div>
                    <div class="smo-pool-bar-wrap">
                        <div class="smo-pool-bar" style="width:${pct}%;background:${barColor}"></div>
                    </div>
                    <div class="smo-pool-stats">
                        <span>${u.total ? fmt(u.used || 0) + ' / ' + fmt(u.total) : t('Odmontowana')}</span>
                        <span style="font-weight:600">${u.total ? pct + '%' : '—'}</span>
                    </div>
                    <div class="smo-pool-meta">
                        <span><i class="fas fa-hdd"></i> ${(p.disks || []).length} ${t('dysków')}</span>
                        <span><i class="fas fa-folder"></i> ${p.mount_path}</span>
                        ${(p.shares || []).length ? '<span><i class="fas fa-share-alt"></i> ' + (p.shares || []).length + ' ' + t('udziałów') + '</span>' : ''}
                    </div>
                </div>`;
            }

            html += '</div></div>';
        } else {
            html += `<div class="smo-section smo-empty-pools">
                <div style="text-align:center;padding:30px 20px">
                    <i class="fas fa-layer-group" style="font-size:48px;color:var(--text-muted);margin-bottom:16px;display:block"></i>
                    <div style="font-size:15px;font-weight:600;margin-bottom:6px">${t('Brak pul storage')}</div>
                    <div style="font-size:13px;color:var(--text-secondary);margin-bottom:16px">${t('Utwórz pulę, aby połączyć dyski i udostępnić je w sieci.')}</div>
                    <button class="spw-btn spw-btn-primary" id="smo-create-pool"><i class="fas fa-magic"></i> ${t('Utwórz pulę')}</button>
                </div>
            </div>`;
        }

        // Physical disks table
        if (physDisks.length) {
            const drMap = {};
            for (const dr of drDisks) drMap[dr.name] = dr;

            html += `<div class="smo-section">
                <div class="smo-section-header">
                    <h4><i class="fas fa-hdd"></i> ${t('Dyski fizyczne')}</h4>
                    <button class="smo-link-btn" id="smo-go-disks">${t('Zarządzaj')} <i class="fas fa-arrow-right"></i></button>
                </div>
                <table class="smo-table">
                <thead><tr>
                    <th>${t('Dysk')}</th><th>${t('Model')}</th>
                    <th>${t('Pojemność')}</th><th>${t('Interfejs')}</th>
                    <th>${t('Temp')}</th><th>SMART</th>
                </tr></thead><tbody>`;
            for (const d of physDisks) {
                const dr = drMap[d.name] || {};
                const temp = dr.temperature ? dr.temperature + '°C' : '—';
                const tempColor = (dr.temperature && dr.temperature > 50) ? '#e74c3c' : 'var(--text-secondary)';
                const health = dr.smart_healthy === true
                    ? '<span style="color:#2ecc71">✓ ' + t('OK') + '</span>'
                    : (dr.smart_available ? '<span style="color:#e74c3c">⚠ ' + t('Uwaga') + '</span>' : '—');
                html += `<tr>
                    <td style="font-weight:600">${d.name || '?'}</td>
                    <td style="color:var(--text-secondary)">${d.model || '—'}</td>
                    <td>${d.size || '—'}</td>
                    <td style="color:var(--text-secondary)">${(d.tran || '—').toUpperCase()}</td>
                    <td style="color:${tempColor}">${temp}</td>
                    <td>${health}</td>
                </tr>`;
            }
            html += '</tbody></table></div>';
        }

        root.innerHTML = html;

        // Attach navigation
        root.querySelector('#smo-go-pools')?.addEventListener('click', () => switchSection('pools'));
        root.querySelector('#smo-go-disks')?.addEventListener('click', () => switchSection('disks'));
        root.querySelector('#smo-create-pool')?.addEventListener('click', () => switchSection('wizard'));
    }
    load();
    return null;
}

/* ═══════════════════════════════════════════════════════════
   Section: Pools (Synology-style pool management)
   ═══════════════════════════════════════════════════════════ */
function _smPools(el, switchSection) {
    const state = { pools: [], selected: null, loading: true };

    const fmt = (b) => {
        if (!b || b <= 0) return '0 B';
        if (b >= 1e12) return (b/1e12).toFixed(1) + ' TB';
        if (b >= 1e9) return (b/1e9).toFixed(1) + ' GB';
        return (b/1e6).toFixed(0) + ' MB';
    };

    async function loadPools() {
        state.loading = true;
        render();
        const data = await api('/storage/pool/list');
        state.pools = (data.pools || []);
        state.loading = false;
        if (state.selected && !state.pools.find(p => p.name === state.selected)) {
            state.selected = null;
        }
        render();
    }

    function render() {
        if (state.loading) {
            el.innerHTML = '<div style="padding:40px;text-align:center;color:var(--text-secondary)"><i class="fas fa-spinner fa-spin fa-2x"></i><div style="margin-top:12px">' + t('Ładowanie pul...') + '</div></div>';
            return;
        }

        if (!state.pools.length) {
            el.innerHTML = `<div style="padding:60px 20px;text-align:center">
                <i class="fas fa-layer-group" style="font-size:64px;color:var(--text-muted);margin-bottom:20px;display:block"></i>
                <div style="font-size:18px;font-weight:700;margin-bottom:8px">${t('Brak pul storage')}</div>
                <div style="font-size:14px;color:var(--text-secondary);margin-bottom:24px;max-width:400px;margin-left:auto;margin-right:auto">
                    ${t('Pule storage łączą dyski fizyczne w jedną przestrzeń z ochroną danych (RAID) i udostępnianiem sieciowym.')}
                </div>
                <button class="spw-btn spw-btn-primary" id="smp-create"><i class="fas fa-magic"></i> ${t('Utwórz pierwszą pulę')}</button>
            </div>`;
            el.querySelector('#smp-create')?.addEventListener('click', () => switchSection('wizard'));
            return;
        }

        let html = `<div class="smp-layout">
            <div class="smp-list">
                <div class="smp-toolbar">
                    <h3 style="margin:0;font-size:15px"><i class="fas fa-layer-group"></i> ${t('Pule storage')} <span style="color:var(--text-secondary);font-weight:400">(${state.pools.length})</span></h3>
                    <div style="display:flex;gap:8px">
                        <button class="smp-btn" id="smp-refresh" title="${t('Odśwież')}"><i class="fas fa-sync-alt"></i></button>
                        <button class="smp-btn smp-btn-accent" id="smp-create"><i class="fas fa-plus"></i> ${t('Nowa pula')}</button>
                    </div>
                </div>`;

        for (const p of state.pools) {
            const u = p.usage || {};
            const pct = u.percent || 0;
            const barColor = pct > 90 ? '#e74c3c' : pct > 70 ? '#f59e0b' : '#10b981';
            const isActive = state.selected === p.name;
            const raidLabel = p.system ? t('Dysk systemowy') : (p.raid_level ? 'RAID ' + p.raid_level : t('Pojedynczy dysk'));
            const statusColor = p.mounted ? '#2ecc71' : '#95a5a6';
            const statusLabel = p.mounted ? t('Aktywna') : t('Odmontowana');
            const diskCount = (p.disks || []).length;
            const shareCount = (p.shares || []).length;
            const poolIcon = p.system ? 'fa-server' : 'fa-database';
            const poolIconColor = p.system ? '#6366f1' : '#3b82f6';

            html += `<div class="smp-card ${isActive ? 'smp-card-active' : ''}" data-pool="${p.name}">
                <div class="smp-card-top">
                    <div class="smp-card-name">
                        <i class="fas ${poolIcon}" style="color:${poolIconColor};margin-right:8px"></i>
                        <strong>${p.name}</strong>
                    </div>
                    <div class="smp-card-status" style="color:${statusColor}">
                        <span class="smo-dot" style="background:${statusColor}"></span> ${statusLabel}
                    </div>
                </div>
                <div class="smp-card-badges">
                    <span class="smo-badge smo-badge-blue">${raidLabel}</span>
                    <span class="smo-badge">${p.fstype || 'ext4'}</span>
                    <span class="smo-badge"><i class="fas fa-hdd"></i> ${diskCount}</span>
                    ${shareCount ? '<span class="smo-badge"><i class="fas fa-share-alt"></i> ' + shareCount + '</span>' : ''}
                </div>
                <div class="smo-pool-bar-wrap" style="margin:10px 0 6px">
                    <div class="smo-pool-bar" style="width:${pct}%;background:${barColor}"></div>
                </div>
                <div class="smp-card-usage">
                    ${u.total ? fmt(u.used || 0) + ' / ' + fmt(u.total) : t('Niedostępna')}
                    <span style="font-weight:600">${u.total ? pct + '%' : ''}</span>
                </div>
            </div>`;
        }

        html += '</div>';

        // Detail panel
        html += '<div class="smp-detail" id="smp-detail">';
        if (state.selected) {
            html += renderDetail(state.pools.find(p => p.name === state.selected));
        } else {
            html += `<div class="smp-detail-empty">
                <i class="fas fa-arrow-left" style="font-size:32px;color:var(--text-muted);margin-bottom:12px"></i>
                <div>${t('Wybierz pulę, aby zobaczyć szczegóły')}</div>
            </div>`;
        }
        html += '</div></div>';

        el.innerHTML = html;
        attachHandlers();
    }

    function renderDetail(pool) {
        if (!pool) return '';
        const u = pool.usage || {};
        const pct = u.percent || 0;
        const barColor = pct > 90 ? '#e74c3c' : pct > 70 ? '#f59e0b' : '#10b981';
        const raidLabel = pool.system ? t('Dysk systemowy') : (pool.raid_level ? 'RAID ' + pool.raid_level : t('Brak (pojedynczy dysk)'));
        const statusColor = pool.mounted ? '#2ecc71' : '#95a5a6';
        const statusLabel = pool.mounted ? t('Aktywna') : t('Odmontowana');
        const poolIcon = pool.system ? 'fa-server' : 'fa-database';
        const poolIconColor = pool.system ? '#6366f1' : '#3b82f6';

        let html = `<div class="smp-detail-header">
            <h3><i class="fas ${poolIcon}" style="color:${poolIconColor};margin-right:8px"></i>${pool.name}</h3>
            <span class="smp-card-status" style="color:${statusColor}"><span class="smo-dot" style="background:${statusColor}"></span> ${statusLabel}</span>
        </div>`;

        if (pool.system) {
            html += `<div style="padding:8px 12px;background:rgba(99,102,241,0.08);border:1px solid rgba(99,102,241,0.2);border-radius:8px;font-size:13px;color:var(--text-secondary);margin-bottom:12px">
                <i class="fas fa-info-circle" style="color:#6366f1;margin-right:6px"></i>
                ${t('Wolumen systemowy — przechowuje dane aplikacji, konfigurację i katalogi domowe użytkowników.')}
            </div>`;
        }

        // Capacity section
        html += `<div class="smp-detail-section">
            <h4>${t('Pojemność')}</h4>
            <div class="smo-pool-bar-wrap" style="height:12px;border-radius:6px;margin:8px 0">
                <div class="smo-pool-bar" style="width:${pct}%;background:${barColor};border-radius:6px"></div>
            </div>
            <div style="display:flex;justify-content:space-between;font-size:13px;color:var(--text-secondary)">
                <span>${t('Użyte')}: <strong style="color:var(--text-primary)">${fmt(u.used || 0)}</strong></span>
                <span>${t('Wolne')}: <strong style="color:var(--text-primary)">${fmt(u.free || 0)}</strong></span>
                <span>${t('Razem')}: <strong style="color:var(--text-primary)">${fmt(u.total || 0)}</strong></span>
            </div>
        </div>`;

        // Configuration
        html += `<div class="smp-detail-section">
            <h4>${t('Konfiguracja')}</h4>
            <div class="smp-info-grid">
                <div class="smp-info-row"><span>${t('RAID')}</span><span>${raidLabel}</span></div>
                <div class="smp-info-row"><span>${t('System plików')}</span><span>${pool.fstype || 'ext4'}</span></div>
                <div class="smp-info-row"><span>${t('Punkt montowania')}</span><span><code>${pool.mount_path || '—'}</code></span></div>
                <div class="smp-info-row"><span>${t('Urządzenie')}</span><span><code>${pool.device || '—'}</code></span></div>
                <div class="smp-info-row"><span>${t('Utworzona')}</span><span>${pool.system ? t('Instalacja systemu') : (pool.created ? new Date(pool.created).toLocaleString() : '—')}</span></div>
            </div>
        </div>`;

        // Member disks
        if (pool.disks && pool.disks.length) {
            html += `<div class="smp-detail-section">
                <h4><i class="fas fa-hdd"></i> ${t('Dyski')} (${pool.disks.length})</h4>
                <div class="smp-disk-chips">
                    ${pool.disks.map(d => `<span class="smp-disk-chip"><i class="fas fa-hdd"></i> /dev/${d}</span>`).join('')}
                </div>
            </div>`;
        }

        // Shares
        const shares = pool.shares || [];
        html += `<div class="smp-detail-section">
            <h4><i class="fas fa-share-alt"></i> ${t('Udziały sieciowe')} (${shares.length})</h4>`;
        if (shares.length) {
            html += '<div class="smp-shares-list">';
            for (const sh of shares) {
                html += `<div class="smp-share-row">
                    <i class="fab fa-windows" style="color:#0078d4"></i>
                    <span>${sh.name}</span>
                    <span style="color:var(--text-secondary);font-size:12px">${sh.path || ''}</span>
                </div>`;
            }
            html += '</div>';
        } else {
            html += `<div style="font-size:13px;color:var(--text-secondary);padding:8px 0">${t('Brak udziałów. Przejdź do Udostępnianie, aby utworzyć.')}</div>`;
        }
        html += '</div>';

        // Actions (hide delete for system volume)
        html += `<div class="smp-detail-actions">
            <button class="smp-btn smp-btn-accent" id="smp-goto-sharing"><i class="fas fa-share-alt"></i> ${t('Udostępnij')}</button>
            ${!pool.system ? `<button class="smp-btn smp-btn-danger" id="smp-delete" data-pool="${pool.name}"><i class="fas fa-trash"></i> ${t('Usuń pulę')}</button>` : ''}
        </div>`;

        return html;
    }

    function attachHandlers() {
        el.querySelectorAll('.smp-card').forEach(card => {
            card.addEventListener('click', () => {
                state.selected = card.dataset.pool;
                render();
            });
        });

        el.querySelector('#smp-refresh')?.addEventListener('click', loadPools);
        el.querySelector('#smp-create')?.addEventListener('click', () => switchSection('wizard'));
        el.querySelector('#smp-goto-sharing')?.addEventListener('click', () => switchSection('sharing'));

        const delBtn = el.querySelector('#smp-delete');
        if (delBtn) {
            delBtn.addEventListener('click', async () => {
                const name = delBtn.dataset.pool;
                const ok = await confirmDialog(
                    t('Usuń pulę') + ': ' + name,
                    t('Czy na pewno chcesz usunąć tę pulę? Dane na dyskach mogą zostać utracone.')
                );
                if (!ok) return;

                const wipe = await confirmDialog(t('Wyczyścić dyski?'),
                    t('Czy chcesz wyczyścić sygnatury na dyskach? Wybierz TAK, jeśli chcesz ponownie użyć dysków w nowej puli.'));

                delBtn.disabled = true;
                delBtn.innerHTML = '<i class="fas fa-spinner fa-spin"></i> ' + t('Usuwanie...');
                const r = await api(`/storage/pool/${encodeURIComponent(name)}/delete`, {
                    method: 'POST', body: { wipe }
                });
                if (r.error) {
                    toast(r.error, 'error');
                } else {
                    toast(t('Pula usunięta'), 'success');
                    state.selected = null;
                    await loadPools();
                }
            });
        }
    }

    loadPools();
    return null;
}

/* ═══════════════════════════════════════════════════════════
   Section: Disks (from original storage.js)
   ═══════════════════════════════════════════════════════════ */
function _smDisks(el) {
    /* el replaces original "body" */
    const body = el;
    const state = { drives: [], selected: null, systemVisible: false, keepalive: {} };

    body.innerHTML = `
    <div class="storage-app">
        <div class="storage-toolbar">
            <button class="fm-toolbar-btn" id="st-refresh" title="${t('Odśwież')}"><i class="fas fa-sync-alt"></i></button>
            <div class="fm-toolbar-sep"></div>
            <span class="storage-status" id="st-status">${t('Ładowanie...')}</span>
        </div>

        <!-- Drive card groups -->
        <div class="st-groups" id="st-groups">
            <div class="sto-center-lg">
                <i class="fas fa-spinner fa-spin sto-spinner"></i>
                <div class="sto-load-text">${t('Ładowanie dysków...')}</div>
            </div>
        </div>

        <!-- Action panel (shown when a drive is selected) -->
        <div class="st-actions" id="st-actions" style="display:none">
            <div class="st-actions-header">
                <span class="st-actions-title" id="st-actions-title"></span>
                <button class="fm-toolbar-btn btn-sm" id="st-actions-close" title="Zamknij"><i class="fas fa-times"></i></button>
            </div>
            <div class="storage-form-row" id="st-mount-row">
                <label>Punkt montowania:</label>
                <input type="text" id="st-mountpoint" placeholder="/media/devmon/dysk" class="fm-input" list="st-mount-suggestions">
                <datalist id="st-mount-suggestions"></datalist>

            </div>
            <div class="st-actions-btns">
                <button class="fm-toolbar-btn btn-green" id="st-mount-btn"><i class="fas fa-link"></i> Montuj</button>
                <button class="fm-toolbar-btn btn-red" id="st-unmount-btn"><i class="fas fa-unlink"></i> Odmontuj</button>
                <button class="fm-toolbar-btn" id="st-label-btn"><i class="fas fa-tag"></i> Etykieta</button>
                <button class="fm-toolbar-btn btn-orange" id="st-format-btn"><i class="fas fa-eraser"></i> Formatuj</button>
                <button class="fm-toolbar-btn btn-purple" id="st-eject-btn"><i class="fas fa-eject"></i> ${t('Wysuń USB')}</button>
                <button class="fm-toolbar-btn" id="st-keepalive-btn"><i class="fas fa-heartbeat"></i> Utrzymuj</button>
                <button class="fm-toolbar-btn" id="st-smart-btn"><i class="fas fa-heartbeat"></i> SMART</button>
                <button class="fm-toolbar-btn btn-purple" id="st-merge-btn"><i class="fas fa-object-group"></i> ${t('Połącz')}</button>
                <button class="fm-toolbar-btn btn-cyan" id="st-split-btn"><i class="fas fa-columns"></i> Podziel</button>
                <button class="fm-toolbar-btn btn-red" id="st-encrypt-btn"><i class="fas fa-lock"></i> LUKS</button>
            </div>
        </div>



        <!-- Format Modal -->
        <div class="modal-overlay st-format-modal" id="st-format-modal" style="display:none">
            <div class="modal sto-modal-lg">
                <div class="modal-header">
                    <span><i class="fas fa-eraser sto-hdr-icon-orange"></i>Formatowanie dysku</span>
                    <button class="btn sto-close-btn" id="st-fmt-close">&times;</button>
                </div>
                <div class="modal-body" id="st-fmt-body">
                    <div class="st-fmt-loading"><i class="fas fa-spinner fa-spin sto-spinner"></i></div>
                </div>
                <div class="modal-footer" id="st-fmt-footer">
                    <button class="btn" id="st-fmt-cancel">Anuluj</button>
                    <button class="btn btn-danger" id="st-fmt-confirm"><i class="fas fa-eraser"></i> Formatuj</button>
                </div>
            </div>
        </div>

        <!-- SMART Detail Modal -->
        <div class="modal-overlay" id="st-smart-modal" style="display:none">
            <div class="modal sto-modal-md">
                <div class="modal-header">
                    <span><i class="fas fa-heartbeat sto-hdr-icon-green"></i>SMART — Zdrowie dysku</span>
                    <button class="btn sto-close-btn" id="st-smart-close">&times;</button>
                </div>
                <div class="modal-body sto-smart-scroll" id="st-smart-body">
                    <div class="sto-center-md"><i class="fas fa-spinner fa-spin sto-spinner"></i></div>
                </div>
                <div class="modal-footer"><button class="btn" id="st-smart-ok">Zamknij</button></div>
            </div>
        </div>
    </div>`;

    const $ = id => body.querySelector(id);
    const statusEl = $('#st-status');

    /* ═══════════════════════════════════════════════════════
       SMART Detail Modal
       ═══════════════════════════════════════════════════════ */
    const smartModal = $('#st-smart-modal');
    const smartBody = $('#st-smart-body');
    $('#st-smart-close').onclick = () => smartModal.style.display = 'none';
    $('#st-smart-ok').onclick = () => smartModal.style.display = 'none';
    smartModal.onclick = (e) => { if (e.target === smartModal) smartModal.style.display = 'none'; };

    async function showSmartDetail(diskName) {
        smartModal.style.display = '';
        smartBody.innerHTML = '<div class="sto-center-md"><i class="fas fa-spinner fa-spin sto-spinner"></i></div>';
        try {
            const [info, scoreData] = await Promise.all([
                api(`/storage/smart?disk=${encodeURIComponent(diskName)}`),
                api(`/diskrepair/smart/${encodeURIComponent(diskName)}/score`).catch(() => null),
            ]);
            if (!info.available) {
                smartBody.innerHTML = `<div class="sto-info-msg"><i class="fas fa-info-circle sto-icon-lg"></i><div class="sto-mt-sm">${t('SMART niedostępny dla tego dysku')}</div></div>`;
                return;
            }
            const hColor = info.health === 'PASSED' ? '#10b981' : '#ef4444';
            const tColor = (info.temperature || 0) > 50 ? '#ef4444' : (info.temperature || 0) > 40 ? '#eab308' : '#10b981';
            const rColor = (info.reallocated_sectors || 0) > 0 ? '#ef4444' : '#10b981';
            const poh = info.power_on_hours ? `${Math.floor(info.power_on_hours / 24)} dni (${info.power_on_hours.toLocaleString()} godz.)` : '-';

            // Health score ring
            const score = scoreData?.score ?? null;
            const grade = scoreData?.grade || '';
            const scoreColor = score >= 90 ? '#10b981' : score >= 70 ? '#3b82f6' : score >= 50 ? '#eab308' : '#ef4444';
            const scoreGradient = score != null ? `conic-gradient(${scoreColor} ${score * 3.6}deg, rgba(255,255,255,0.06) 0deg)` : 'none';
            const scoreHtml = score != null ? `
                <div class="sto-smart-card" style="background:rgba(${score >= 70 ? '16,185,129' : score >= 50 ? '234,179,8' : '239,68,68'},.08)">
                    <div class="sto-stat-label">${t('Zdrowie')}</div>
                    <div style="display:flex;align-items:center;gap:10px">
                        <div style="width:48px;height:48px;border-radius:50%;background:${scoreGradient};display:flex;align-items:center;justify-content:center">
                            <div style="width:38px;height:38px;border-radius:50%;background:var(--bg-card,#1e293b);display:flex;align-items:center;justify-content:center;font-size:16px;font-weight:700;color:${scoreColor}">${score}</div>
                        </div>
                        <div style="font-size:12px;color:var(--text-muted);text-transform:uppercase">${grade}</div>
                    </div>
                </div>` : '';

            smartBody.innerHTML = `
                <div class="sto-smart-grid" style="grid-template-columns:repeat(${score != null ? 3 : 2},1fr)">
                    ${scoreHtml}
                    <div class="sto-smart-card" style="background:rgba(${info.health === 'PASSED' ? '16,185,129' : '239,68,68'},.08)">
                        <div class="sto-stat-label">Status</div>
                        <div class="sto-stat-value" style="color:${hColor}">${info.health || '?'}</div>
                    </div>
                    <div class="sto-smart-card" style="background:rgba(${tColor === '#ef4444' ? '239,68,68' : tColor === '#eab308' ? '234,179,8' : '16,185,129'},.08)">
                        <div class="sto-stat-label">Temperatura</div>
                        <div class="sto-stat-value" style="color:${tColor}">${info.temperature != null ? info.temperature + '°C' : '-'}</div>
                    </div>
                </div>
                <table class="sto-smart-table">
                    <tr class="sto-tr-border"><td class="sto-td-label">Model</td><td class="sto-td-value">${info.model || '-'}</td></tr>
                    <tr class="sto-tr-border"><td class="sto-td-label">Numer seryjny</td><td class="sto-td-mono">${info.serial || '-'}</td></tr>
                    <tr class="sto-tr-border"><td class="sto-td-label">Firmware</td><td class="sto-td-right">${info.firmware || '-'}</td></tr>
                    <tr class="sto-tr-border"><td class="sto-td-label">Czas pracy</td><td class="sto-td-right">${poh}</td></tr>
                    <tr><td class="sto-td-label">Realokowane sektory</td><td class="sto-td-right-bold" style="color:${rColor}">${info.reallocated_sectors != null ? info.reallocated_sectors : '-'}</td></tr>
                </table>
                ${(info.reallocated_sectors || 0) > 0 ? `<div class="sto-alert-danger"><i class="fas fa-exclamation-triangle sto-mr-xs"></i>${t('Dysk ma realokowane sektory — rozważ wymianę!')}</div>` : ''}
                <div style="margin-top:12px;display:flex;gap:8px">
                    <button class="fm-toolbar-btn btn-sm" id="st-smart-selftest"><i class="fas fa-vial"></i> ${t('Self-test')}</button>
                    <button class="fm-toolbar-btn btn-sm" id="st-smart-detail"><i class="fas fa-list"></i> ${t('Atrybuty')}</button>
                </div>
            `;

            smartBody.querySelector('#st-smart-selftest')?.addEventListener('click', async () => {
                try {
                    const r = await api('/diskrepair/smart-test', { method: 'POST', body: { disk: diskName, type: 'short' } });
                    if (r.error) { toast(r.error, 'error'); return; }
                    toast(t('Self-test uruchomiony') + (r.estimated_minutes ? ` (~${r.estimated_minutes} min)` : ''), 'success');
                } catch (e) { toast(e.message, 'error'); }
            });

            smartBody.querySelector('#st-smart-detail')?.addEventListener('click', async () => {
                try {
                    const detail = await api(`/diskrepair/smart/${encodeURIComponent(diskName)}`);
                    if (detail.error) { toast(detail.error, 'error'); return; }
                    const attrs = detail.attributes || [];
                    if (!attrs.length) { toast(t('Brak atrybutów SMART'), 'info'); return; }
                    showModal(t('Atrybuty SMART — ') + diskName, `
                        <div style="max-height:400px;overflow-y:auto">
                            <table style="width:100%;font-size:12px;border-collapse:collapse">
                                <tr style="background:var(--bg-card);font-weight:600;text-align:left">
                                    <th style="padding:6px">ID</th><th style="padding:6px">${t('Atrybut')}</th>
                                    <th style="padding:6px">Val</th><th style="padding:6px">Worst</th>
                                    <th style="padding:6px">Thresh</th><th style="padding:6px">Raw</th>
                                    <th style="padding:6px">Status</th>
                                </tr>
                                ${attrs.map(a => {
                                    const sc = a.status === 'failing' ? 'color:#ef4444;font-weight:600' : a.status === 'warn' ? 'color:#eab308' : '';
                                    return `<tr style="border-bottom:1px solid var(--border)">
                                        <td style="padding:4px 6px">${a.id}</td><td style="padding:4px 6px">${a.name}</td>
                                        <td style="padding:4px 6px">${a.value}</td><td style="padding:4px 6px">${a.worst}</td>
                                        <td style="padding:4px 6px">${a.thresh}</td><td style="padding:4px 6px;font-family:monospace">${a.raw}</td>
                                        <td style="padding:4px 6px;${sc}">${a.status}</td>
                                    </tr>`;
                                }).join('')}
                            </table>
                        </div>
                    `, [{ label: t('Zamknij'), class: 'secondary' }]);
                } catch (e) { toast(e.message, 'error'); }
            });
        } catch (e) {
            smartBody.innerHTML = `<div class="sto-error-msg">${t('Błąd:')} ${e.message}</div>`;
        }
    }

    /* ═══════════════════════════════════════════════════════
       Load & Render Drives (card layout)
       ═══════════════════════════════════════════════════════ */
    async function loadDrives() {
        statusEl.textContent = t('Ładowanie...');
        try {
            const data = await api('/storage/drives');
            state.drives = Array.isArray(data) ? data : (data.drives || []);
            // Load keepalive status
            try {
                const ka = await api('/storage/keepalive');
                state.keepalive = ka.drives || {};
            } catch(e) { state.keepalive = {}; }
            renderDrives();
            const partCount = state.drives.filter(d => d.type !== 'disk').length;
            statusEl.textContent = `${partCount} partycji`;
        } catch (e) { statusEl.textContent = t('Błąd ładowania'); }
        // mount path suggestions
        const sl = $('#st-mount-suggestions');
        if (sl) {
            const paths = new Set(['/media/devmon/']);
            state.drives.forEach(d => {
                if (d.label) paths.add(`/media/devmon/${d.label}`);
                if (d.mountpoint && d.mountpoint !== '/') paths.add(d.mountpoint);
            });
            sl.innerHTML = [...paths].map(p => `<option value="${p}">`).join('');
        }
    }

    function renderDrives() {
        const container = $('#st-groups');
        // Only show partitions / LVM / raw disks — hide parent disks that have children
        const visible = state.drives.filter(d => !(d.type === 'disk' && d.children_count > 0));

        const groupDefs = [
            { key: 'usb',     icon: 'fa-usb',    color: '#a78bfa', label: 'USB' },
            { key: 'storage', icon: 'fa-hdd',     color: '#f59e0b', label: t('Magazyn') },
            { key: 'system',  icon: 'fa-server',  color: '#3b82f6', label: t('System') },
        ];

        let html = '';
        for (const g of groupDefs) {
            const items = visible.filter(d => d.category === g.key);
            if (!items.length) continue;
            const isSystem = g.key === 'system';
            const collapsed = isSystem && !state.systemVisible;
            html += `
            <div class="st-group">
                <div class="st-group-header${isSystem ? ' st-group-toggle' : ''}">
                    <i class="fas ${g.icon} sto-group-icon" style="color:${g.color}"></i>
                    <span class="st-group-label">${g.label}</span>
                    <span class="st-group-count">${items.length}</span>
                    ${isSystem ? `<i class="fas fa-chevron-${collapsed ? 'right' : 'down'} st-group-chevron"></i>` : ''}
                </div>
                <div class="st-cards" ${collapsed ? 'style="display:none"' : ''}>
                    ${items.map(d => renderCard(d)).join('')}
                </div>
            </div>`;
        }

        container.innerHTML = html;
        bindCardEvents();
    }

    function renderCard(d) {
        const parent = d.parent ? state.drives.find(x => x.name === d.parent) : null;
        const model = parent?.model || d.model || '';
        const label = d.label || d.name;
        const sel = state.selected === d.name;

        // Usage bar
        const pct = d.usage?.percent || 0;
        const pctColor = pct > 90 ? '#ef4444' : pct > 70 ? '#eab308' : '#10b981';
        const usageHtml = d.usage ? `
            <div class="st-card-usage">
                <div class="st-card-bar"><div class="st-card-bar-fill" style="width:${pct}%;background:${pctColor}"></div></div>
                <span class="st-card-pct" style="color:${pctColor}">${Math.round(pct)}%</span>
            </div>` : '';

        // Mount point
        const mountHtml = d.mountpoint
            ? `<div class="st-card-mount"><i class="fas fa-folder-open"></i> ${d.mountpoint}</div>`
            : `<div class="st-card-mount st-unmounted"><i class="fas fa-minus-circle"></i> Niezamontowany</div>`;

        // Badges
        const badges = [];
        if (d.mountpoint) badges.push('<span class="fm-badge fm-badge-green">Zamontowany</span>');
        else badges.push('<span class="fm-badge fm-badge-dim">Odmontowany</span>');
        if (state.keepalive[d.name]) {
            badges.push('<span class="fm-badge sto-badge-keepalive"><i class="fas fa-shield-alt sto-badge-sm"></i> Utrzymywany</span>');
        }
        if (d.smart_temp != null) {
            const sc = d.smart_temp > 50 ? '#ef4444' : d.smart_temp > 40 ? '#eab308' : '#10b981';
            badges.push(`<span class="fm-badge st-smart-badge" data-smart-disk="${d.parent || d.name}" style="background:rgba(${d.smart_temp > 50 ? '239,68,68' : d.smart_temp > 40 ? '234,179,8' : '16,185,129'},.12);color:${sc};cursor:pointer" title="SMART: ${d.smart_health || '?'}"><i class="fas fa-thermometer-half sto-badge-sm"></i> ${d.smart_temp}°C</span>`);
        }

        const catIcon = d.category === 'system' ? 'fa-server' : d.category === 'usb' ? 'fa-usb' : 'fa-hdd';
        const catColor = d.category === 'system' ? '#3b82f6' : d.category === 'usb' ? '#a78bfa' : '#f59e0b';

        return `
        <div class="st-card${sel ? ' st-card-selected' : ''}" data-dev="${d.name}">
            <div class="st-card-header">
                <i class="fas ${catIcon}" style="color:${catColor}"></i>
                <div>
                    <div class="st-card-name">${label}</div>
                    <div class="st-card-sub">/dev/${d.name} · ${d.fstype || '—'} · ${d.size || '—'}</div>
                    ${model ? `<div class="st-card-sub">${model}</div>` : ''}
                </div>
            </div>
            ${usageHtml}
            ${mountHtml}
            <div class="st-card-badges">${badges.join(' ')}</div>
        </div>`;
    }

    function bindCardEvents() {
        body.querySelectorAll('.st-card').forEach(card => {
            card.onclick = (e) => {
                if (e.target.closest('.st-smart-badge')) {
                    showSmartDetail(e.target.closest('.st-smart-badge').dataset.smartDisk);
                    return;
                }
                const dev = card.dataset.dev;
                state.selected = state.selected === dev ? null : dev;
                renderDrives();
                updateActions();
            };
        });
        // System group toggle
        body.querySelectorAll('.st-group-toggle').forEach(hdr => {
            hdr.onclick = () => {
                state.systemVisible = !state.systemVisible;
                renderDrives();
                updateActions();
            };
        });
    }

    /* ═══════════════════════════════════════════════════════
       Action Panel
       ═══════════════════════════════════════════════════════ */
    function updateActions() {
        const panel = $('#st-actions');
        if (!state.selected) { panel.style.display = 'none'; return; }
        const drive = state.drives.find(d => d.name === state.selected);
        if (!drive) { panel.style.display = 'none'; return; }

        panel.style.display = '';
        const parent = drive.parent ? state.drives.find(d => d.name === drive.parent) : null;
        const isDisk = drive.type === 'disk';
        const isSystem = drive.category === 'system';
        const isUsb = drive.category === 'usb' || parent?.category === 'usb';
        const parentDisk = parent || (isDisk ? drive : null);
        const siblings = parentDisk ? state.drives.filter(d => d.parent === parentDisk.name) : [];
        const hasSmart = drive.smart_health || parent?.smart_health;

        $('#st-actions-title').innerHTML = `<i class="fas fa-hdd sto-action-icon"></i>${drive.label || drive.name} <span class="sto-action-sub">/dev/${drive.name}</span>`;

        // Mount row
        $('#st-mount-row').style.display = (isDisk || isSystem) ? 'none' : '';
        if (!isDisk && !isSystem) {
            $('#st-mountpoint').value = drive.mountpoint || `/media/devmon/${drive.label || drive.name}`;
        }

        // Button visibility
        const kaActive = !!state.keepalive[drive.name];
        $('#st-mount-btn').style.display = (!isDisk && !isSystem && !drive.mountpoint) ? '' : 'none';
        $('#st-unmount-btn').style.display = (!isDisk && !isSystem && drive.mountpoint) ? '' : 'none';
        $('#st-format-btn').style.display = isSystem ? 'none' : '';
        $('#st-label-btn').style.display = (isDisk || isSystem) ? 'none' : '';
        $('#st-eject-btn').style.display = (isUsb && parentDisk) ? '' : 'none';
        $('#st-keepalive-btn').style.display = (!isDisk && !isSystem && drive.mountpoint) ? '' : 'none';
        const kaBtn = $('#st-keepalive-btn');
        if (kaActive) {
            kaBtn.className = 'fm-toolbar-btn btn-purple';
            kaBtn.innerHTML = '<i class="fas fa-shield-alt"></i> Utrzymuj <span class="sto-ka-on">(ON)</span>';
        } else {
            kaBtn.className = 'fm-toolbar-btn';
            kaBtn.innerHTML = '<i class="fas fa-shield-alt"></i> Utrzymuj';
        }
        $('#st-smart-btn').style.display = hasSmart ? '' : 'none';
        $('#st-merge-btn').style.display = (parentDisk && siblings.length >= 2 && !isSystem) ? '' : 'none';
        $('#st-split-btn').style.display = (parentDisk && !isSystem) ? '' : 'none';
        // Encryption button — show for partitions that are not system
        $('#st-encrypt-btn').style.display = (!isDisk && !isSystem) ? '' : 'none';
        const encBtn = $('#st-encrypt-btn');
        if (drive.fstype === 'crypto_LUKS') {
            encBtn.innerHTML = `<i class="fas fa-lock"></i> ${drive.mountpoint ? t('Zablokuj') : t('Odblokuj')}`;
            encBtn.className = 'fm-toolbar-btn ' + (drive.mountpoint ? 'btn-orange' : 'btn-green');
        } else {
            encBtn.innerHTML = `<i class="fas fa-lock"></i> ${t('Szyfruj')}`;
            encBtn.className = 'fm-toolbar-btn btn-red';
        }
    }

    $('#st-actions-close').onclick = () => {
        state.selected = null;
        renderDrives();
        updateActions();
    };

    // ─── Mount / Unmount ─────────────────────────────────
    $('#st-mount-btn').onclick = async () => {
        if (!state.selected) return;
        const mp = $('#st-mountpoint').value.trim();
        if (!mp) { toast('Podaj punkt montowania', 'warning'); return; }
        try {
            await api('/storage/mount', { method: 'POST', body: { drive: state.selected, path: mp } });
            toast('Zamontowano', 'success');
            loadDrives();
        } catch (e) { toast(t('Błąd montowania: ') + e.message, 'error'); }
    };
    $('#st-unmount-btn').onclick = async () => {
        if (!state.selected) return;
        const drive = state.drives.find(d => d.name === state.selected);
        if (!drive?.mountpoint) { toast('Dysk nie jest zamontowany', 'warning'); return; }
        if (!await confirmDialog(`${t('Odmontować')} ${drive.mountpoint}?`)) return;
        try {
            await api('/storage/unmount', { method: 'POST', body: { path: drive.mountpoint } });
            toast('Odmontowano', 'success');
            loadDrives();
        } catch (e) { toast(t('Błąd odmontowywania: ') + e.message, 'error'); }
    };

    // ─── Relabel drive ─────────────────────────────────────
    $('#st-label-btn').onclick = async () => {
        if (!state.selected) return;
        const drive = state.drives.find(d => d.name === state.selected);
        if (!drive) return;
        const current = drive.label || '';
        const newLabel = prompt(`Nowa etykieta dla /dev/${drive.name}:`, current);
        if (newLabel === null || newLabel === current) return;
        if (!newLabel.trim()) { toast(t('Etykieta nie może być pusta'), 'error'); return; }
        if (newLabel.includes('/') || newLabel.length > 32) { toast('Max 32 znaki, bez /', 'error'); return; }
        try {
            await api('/storage/relabel', { method: 'POST', body: { drive: drive.name, label: newLabel.trim() } });
            toast(`Etykieta zmieniona na "${newLabel.trim()}"`, 'success');
            loadDrives();
        } catch (e) { toast(t('Błąd: ') + e.message, 'error'); }
    };

    // ─── Eject USB ───────────────────────────────────────
    $('#st-eject-btn').onclick = async () => {
        if (!state.selected) return;
        const drive = state.drives.find(d => d.name === state.selected);
        const parent = drive?.parent ? state.drives.find(d => d.name === drive.parent) : drive;
        const diskName = parent?.name || drive?.name;
        if (!await confirmDialog(`${t('Bezpiecznie wysunąć')} /dev/${diskName}?\n${t('Wszystkie partycje zostaną odmontowane.')}`)) return;
        try {
            await api('/storage/eject', { method: 'POST', body: { disk: diskName } });
            toast(t('Dysk bezpiecznie wysunięty'), 'success');
            state.selected = null;
            loadDrives();
        } catch (e) { toast(t('Błąd wysuwania: ') + e.message, 'error'); }
    };

    // ─── Keep-alive toggle ────────────────────────────────
    $('#st-keepalive-btn').onclick = async () => {
        if (!state.selected) return;
        const drive = state.drives.find(d => d.name === state.selected);
        if (!drive || !drive.mountpoint) return;
        const isActive = !!state.keepalive[drive.name];
        const enable = !isActive;
        if (enable) {
            if (!await confirmDialog(`${t('Włączyć auto-remount dla')} ${drive.label || drive.name}?\n${t('Dysk będzie automatycznie montowany ponownie jeśli się odmontuje, a USB autosuspend zostanie wyłączony.')}`)) return;
        }
        try {
            await api('/storage/keepalive', { method: 'POST', body: {
                drive: drive.name,
                mountpoint: drive.mountpoint,
                fstype: drive.fstype || 'auto',
                label: drive.label || drive.name,
                disk: drive.parent || drive.name,
                enable,
            }});
            toast(enable ? t('Utrzymywanie włączone') : t('Utrzymywanie wyłączone'), 'success');
            if (enable) state.keepalive[drive.name] = { mountpoint: drive.mountpoint };
            else delete state.keepalive[drive.name];
            renderDrives();
            updateActions();
        } catch (e) { toast(t('Błąd: ') + e.message, 'error'); }
    };

    // ─── SMART from action panel ─────────────────────────
    $('#st-smart-btn').onclick = () => {
        if (!state.selected) return;
        const drive = state.drives.find(d => d.name === state.selected);
        showSmartDetail(drive?.parent || drive?.name);
    };

    /* ═══════════════════════════════════════════════════════
       Format Modal
       ═══════════════════════════════════════════════════════ */
    const fmtModal = $('#st-format-modal');
    const fmtBody  = $('#st-fmt-body');
    const fmtFooter = $('#st-fmt-footer');
    let fmtOptions = [];
    let fmtSystemFs = 'ext4';

    function closeFmtModal() { fmtModal.style.display = 'none'; }
    $('#st-fmt-close').onclick  = closeFmtModal;
    $('#st-fmt-cancel').onclick = closeFmtModal;
    fmtModal.onclick = (e) => { if (e.target === fmtModal) closeFmtModal(); };

    $('#st-format-btn').onclick = async () => {
        if (!state.selected) return;
        const drive = state.drives.find(d => d.name === state.selected);
        if (!drive) return;
        if (drive.category === 'system') { toast(t('Nie można formatować dysku systemowego!'), 'error'); return; }
        if (drive.mountpoint) { toast('Odmontuj dysk przed formatowaniem!', 'warning'); return; }

        fmtModal.style.display = '';
        fmtBody.innerHTML = `<div class="sto-center-sm"><i class="fas fa-spinner fa-spin sto-spinner"></i><div class="sto-load-text">${t('Ładowanie opcji...')}</div></div>`;
        fmtFooter.style.display = '';
        $('#st-fmt-confirm').disabled = false;
        $('#st-fmt-confirm').style.display = '';

        try {
            const data = await api('/storage/format/options');
            fmtOptions = data.options || [];
            fmtSystemFs = data.system_fs || 'ext4';
            renderFmtForm(drive);
        } catch (e) {
            fmtBody.innerHTML = `<div class="sto-error-msg"><i class="fas fa-exclamation-triangle sto-icon-lg"></i><div class="sto-mt-sm">${t('Błąd:')} ${e.message}</div></div>`;
        }
    };

    function renderFmtForm(drive) {
        const recommended = fmtOptions.find(o => o.recommended);
        const defaultFs = recommended ? recommended.value : 'ext4';
        fmtBody.innerHTML = `
            <div class="st-fmt-drive-info">
                <i class="fas fa-hdd sto-drive-icon-orange"></i>
                <div>
                    <div class="sto-bold">/dev/${drive.name}</div>
                    <div class="sto-subtitle">${drive.size || '?'} ${drive.model ? '· ' + drive.model : ''} ${drive.fstype ? '· Aktualnie: ' + drive.fstype : ''}</div>
                </div>
            </div>
            <div class="st-fmt-field">
                <label class="modal-label">${t('System plików')}</label>
                <div class="st-fmt-fs-list" id="st-fmt-fs-list">
                    ${fmtOptions.map(fs => {
                        const disabled = !fs.available;
                        const osIcons = [fs.linux ? '<i class="fab fa-linux sto-os-linux" title="Linux"></i>' : '', fs.windows ? '<i class="fab fa-windows sto-os-windows" title="Windows"></i>' : '', fs.mac ? '<i class="fab fa-apple sto-os-mac" title="macOS"></i>' : ''].filter(Boolean).join(' ');
                        return `
                        <div class="st-fmt-fs-option ${disabled ? 'disabled' : ''} ${fs.value === defaultFs ? 'selected' : ''}" data-fs="${fs.value}" ${disabled ? `title="${t('Niedostępne')}"` : ''}>
                            <div class="st-fmt-fs-radio"><div class="st-fmt-fs-radio-dot"></div></div>
                            <div class="st-fmt-fs-info">
                                <div class="st-fmt-fs-label">${fs.label}${fs.recommended ? ' <span class="st-fmt-badge-rec"><i class="fas fa-star"></i> ' + t('Rekomendowany') + '</span>' : ''}${disabled ? ` <span class="st-fmt-badge-na">${t('Niedostępny')}</span>` : ''}</div>
                                <div class="st-fmt-fs-desc">${fs.desc}</div>
                                <div class="st-fmt-fs-os">${osIcons}</div>
                            </div>
                        </div>`;
                    }).join('')}
                </div>
            </div>
            <div class="st-fmt-field">
                <label class="modal-label">Etykieta dysku <span class="sto-optional">(opcjonalnie)</span></label>
                <input type="text" class="modal-input" id="st-fmt-label" placeholder="np. Dane, Backup, Media" maxlength="16">
            </div>
            <div class="st-fmt-system-info">
                <i class="fas fa-info-circle sto-icon-accent"></i>
                <span>${t('System używa:')} <b>${fmtSystemFs}</b></span>
            </div>`;
        fmtBody.querySelectorAll('.st-fmt-fs-option:not(.disabled)').forEach(opt => {
            opt.onclick = () => {
                fmtBody.querySelectorAll('.st-fmt-fs-option').forEach(o => o.classList.remove('selected'));
                opt.classList.add('selected');
            };
        });
    }

    $('#st-fmt-confirm').onclick = async () => {
        if (!state.selected) return;
        const selectedOpt = fmtBody.querySelector('.st-fmt-fs-option.selected');
        if (!selectedOpt) { toast(t('Wybierz system plików'), 'warning'); return; }
        const fstype = selectedOpt.dataset.fs;
        const label  = fmtBody.querySelector('#st-fmt-label')?.value.trim() || '';
        const fsLabel = fmtOptions.find(o => o.value === fstype)?.label || fstype;
        if (!await confirmDialog(`⚠️ ${t('UWAGA!')}\n\n${t('Czy na pewno sformatować')} /dev/${state.selected} ${t('na')} ${fsLabel}?\n\n${t('WSZYSTKIE DANE ZOSTANĄ UTRACONE!')}\n${t('Ta operacja jest NIEODWRACALNA!')}`)) return;

        fmtFooter.style.display = 'none';
        fmtBody.innerHTML = `
            <div class="st-fmt-progress">
                <div class="st-fmt-progress-header">
                    <i class="fas fa-cog fa-spin sto-progress-icon-orange"></i>
                    <div><div class="sto-bold">Formatowanie /dev/${state.selected}</div><div class="sto-subtitle">${fsLabel} ${label ? '· ' + label : ''}</div></div>
                </div>
                <div class="st-fmt-log" id="st-fmt-log"></div>
                <div id="st-fmt-result" style="display:none"></div>
            </div>`;
        const logEl = fmtBody.querySelector('#st-fmt-log');
        const resultEl = fmtBody.querySelector('#st-fmt-result');
        function addLog(msg, type = 'info') {
            const icon = type === 'error' ? 'fa-times-circle' : type === 'success' ? 'fa-check-circle' : 'fa-chevron-right';
            const color = type === 'error' ? '#ef4444' : type === 'success' ? '#10b981' : 'var(--text-muted)';
            logEl.innerHTML += `<div class="st-fmt-log-line"><i class="fas ${icon} sto-log-icon" style="color:${color}"></i><span>${msg}</span></div>`;
            logEl.scrollTop = logEl.scrollHeight;
        }
        try {
            const headers = {};
            if (NAS.token) headers['Authorization'] = `Bearer ${NAS.token}`;
            if (NAS.csrfToken) headers['X-CSRFToken'] = NAS.csrfToken;
            headers['Content-Type'] = 'application/json';
            const resp = await fetch('/api/storage/format', { method: 'POST', headers, body: JSON.stringify({ drive: state.selected, fstype, label }) });
            if (!resp.ok) { const err = await resp.json().catch(() => ({})); addLog(err.error || t('Błąd formatowania'), 'error'); showFmtDone(false); return; }
            const reader = resp.body.getReader();
            const decoder = new TextDecoder();
            let buf = '';
            while (true) {
                const { done, value } = await reader.read();
                if (done) break;
                buf += decoder.decode(value, { stream: true });
                const lines = buf.split('\n'); buf = lines.pop();
                for (const line of lines) {
                    if (!line.startsWith('data: ')) continue;
                    try { const ev = JSON.parse(line.slice(6)); if (ev.type === 'step' || ev.type === 'log') addLog(ev.message); if (ev.type === 'done') showFmtDone(ev.success); } catch {}
                }
            }
        } catch (e) { addLog(t('Błąd: ') + e.message, 'error'); showFmtDone(false); }
        function showFmtDone(success) {
            resultEl.style.display = '';
            const cog = fmtBody.querySelector('.fa-cog.fa-spin');
            if (success) {
                resultEl.innerHTML = `<div class="st-fmt-result-ok"><i class="fas fa-check-circle sto-result-ok-icon"></i><div><div class="sto-result-ok-text">${t('Formatowanie zakończone!')}</div><div class="sto-result-sub">${t('Dysk gotowy do użycia.')}</div></div></div>`;
                if (cog) { cog.classList.remove('fa-cog','fa-spin'); cog.classList.add('fa-check-circle'); cog.style.color = '#10b981'; }
            } else {
                resultEl.innerHTML = `<div class="st-fmt-result-err"><i class="fas fa-times-circle sto-result-err-icon"></i><div><div class="sto-result-err-text">${t('Formatowanie nie powiodło się')}</div><div class="sto-result-sub">${t('Sprawdź logi powyżej.')}</div></div></div>`;
                if (cog) { cog.classList.remove('fa-cog','fa-spin'); cog.classList.add('fa-exclamation-triangle'); cog.style.color = '#ef4444'; }
            }
            fmtFooter.style.display = '';
            fmtFooter.innerHTML = '<button class="btn btn-primary" id="st-fmt-done-btn"><i class="fas fa-check"></i> Zamknij</button>';
            fmtFooter.querySelector('#st-fmt-done-btn').onclick = () => { closeFmtModal(); loadDrives(); };
        }
    };

    /* ═══════════════════════════════════════════════════════
       Merge Partitions
       ═══════════════════════════════════════════════════════ */
    $('#st-merge-btn').onclick = async () => {
        if (!state.selected) return;
        const drive = state.drives.find(d => d.name === state.selected);
        const parent = drive?.parent ? state.drives.find(d => d.name === drive.parent) : (drive?.type === 'disk' ? drive : null);
        if (!parent) { toast(t('Nie znaleziono dysku nadrzędnego'), 'warning'); return; }

        const childParts = state.drives.filter(d => d.parent === parent.name);
        if (childParts.length < 2) { toast(t('Dysk ma tylko jedną partycję'), 'warning'); return; }
        if (parent.category === 'system') { toast(t('Nie można łączyć partycji dysku systemowego!'), 'error'); return; }
        const mountedParts = childParts.filter(d => d.mountpoint);
        if (mountedParts.length) { toast(`Odmontuj najpierw: ${mountedParts.map(d => d.name).join(', ')}`, 'warning'); return; }

        fmtModal.style.display = '';
        fmtBody.innerHTML = '<div class="sto-center-sm"><i class="fas fa-spinner fa-spin sto-spinner"></i></div>';
        fmtFooter.style.display = '';
        fmtFooter.innerHTML = `<button class="btn" id="st-merge-cancel2">${t('Anuluj')}</button><button class="btn btn-danger" id="st-merge-confirm"><i class="fas fa-object-group"></i> ${t('Połącz i formatuj')}</button>`;
        fmtFooter.querySelector('#st-merge-cancel2').onclick = closeFmtModal;

        try {
            const data = await api('/storage/format/options');
            fmtOptions = data.options || [];
            fmtSystemFs = data.system_fs || 'ext4';
            const recommended = fmtOptions.find(o => o.recommended);
            const defaultFs = recommended ? recommended.value : 'ext4';
            fmtBody.innerHTML = `
                <div class="st-fmt-drive-info sto-drive-info-merge">
                    <i class="fas fa-object-group sto-drive-icon-purple"></i>
                    <div>
                        <div class="sto-bold">/dev/${parent.name} — ${t('Łączenie partycji')}</div>
                        <div class="sto-subtitle">${parent.size || '?'} ${parent.model ? '· ' + parent.model : ''}</div>
                        <div class="sto-merge-parts"><i class="fas fa-exclamation-triangle"></i> Partycje: ${childParts.map(d => `<b>${d.name}</b> (${d.size})`).join(', ')}</div>
                    </div>
                </div>
                <div class="st-fmt-field">
                    <label class="modal-label">${t('System plików')}</label>
                    <div class="st-fmt-fs-list" id="st-fmt-fs-list">
                        ${fmtOptions.map(fs => {
                            const disabled = !fs.available;
                            const osIcons = [fs.linux ? '<i class="fab fa-linux sto-os-linux" title="Linux"></i>' : '', fs.windows ? '<i class="fab fa-windows sto-os-windows" title="Windows"></i>' : '', fs.mac ? '<i class="fab fa-apple sto-os-mac" title="macOS"></i>' : ''].filter(Boolean).join(' ');
                            return `<div class="st-fmt-fs-option ${disabled ? 'disabled' : ''} ${fs.value === defaultFs ? 'selected' : ''}" data-fs="${fs.value}"><div class="st-fmt-fs-radio"><div class="st-fmt-fs-radio-dot"></div></div><div class="st-fmt-fs-info"><div class="st-fmt-fs-label">${fs.label}${fs.recommended ? ' <span class="st-fmt-badge-rec"><i class="fas fa-star"></i> Rek.</span>' : ''}${disabled ? ' <span class="st-fmt-badge-na">N/D</span>' : ''}</div><div class="st-fmt-fs-desc">${fs.desc}</div><div class="st-fmt-fs-os">${osIcons}</div></div></div>`;
                        }).join('')}
                    </div>
                </div>
                <div class="st-fmt-field">
                    <label class="modal-label">Etykieta <span class="sto-optional">(opcjonalnie)</span></label>
                    <input type="text" class="modal-input" id="st-fmt-label" placeholder="np. Dane" maxlength="16">
                </div>
                <div class="st-fmt-system-info sto-sys-info-danger">
                    <i class="fas fa-exclamation-triangle sto-text-danger"></i>
                    <span class="sto-text-danger"><b>${t('UWAGA:')}</b> ${t('Wszystkie partycje i dane na')} /dev/${parent.name} ${t('zostaną usunięte.')}</span>
                </div>`;
            fmtBody.querySelectorAll('.st-fmt-fs-option:not(.disabled)').forEach(opt => {
                opt.onclick = () => { fmtBody.querySelectorAll('.st-fmt-fs-option').forEach(o => o.classList.remove('selected')); opt.classList.add('selected'); };
            });
        } catch (e) { fmtBody.innerHTML = `<div class="sto-error-msg">${t('Błąd:')} ${e.message}</div>`; return; }

        fmtFooter.querySelector('#st-merge-confirm').onclick = async () => {
            const selectedOpt = fmtBody.querySelector('.st-fmt-fs-option.selected');
            if (!selectedOpt) { toast(t('Wybierz system plików'), 'warning'); return; }
            const fstype = selectedOpt.dataset.fs;
            const label = fmtBody.querySelector('#st-fmt-label')?.value.trim() || '';
            const fsLabel = fmtOptions.find(o => o.value === fstype)?.label || fstype;
            if (!await confirmDialog(`⚠️ ${t('UWAGA!')}\n\n${t('Połączyć partycje na')} /dev/${parent.name}?\n\n${t('Partycje')} ${childParts.map(d => d.name).join(', ')} ${t('zostaną USUNIĘTE.')}\n${t('WSZYSTKIE DANE ZOSTANĄ UTRACONE!')}`)) return;

            fmtFooter.style.display = 'none';
            fmtBody.innerHTML = `<div class="st-fmt-progress"><div class="st-fmt-progress-header"><i class="fas fa-cog fa-spin sto-progress-icon-purple"></i><div><div class="sto-bold">${t('Łączenie')} /dev/${parent.name}</div><div class="sto-subtitle">${fsLabel}</div></div></div><div class="st-fmt-log" id="st-fmt-log"></div><div id="st-fmt-result" style="display:none"></div></div>`;
            const logEl = fmtBody.querySelector('#st-fmt-log');
            const resultEl = fmtBody.querySelector('#st-fmt-result');
            function addLog(msg, type = 'info') {
                const icon = type === 'error' ? 'fa-times-circle' : type === 'success' ? 'fa-check-circle' : 'fa-chevron-right';
                const color = type === 'error' ? '#ef4444' : type === 'success' ? '#10b981' : 'var(--text-muted)';
                logEl.innerHTML += `<div class="st-fmt-log-line"><i class="fas ${icon} sto-log-icon" style="color:${color}"></i><span>${msg}</span></div>`;
                logEl.scrollTop = logEl.scrollHeight;
            }
            try {
                const headers = {}; if (NAS.token) headers['Authorization'] = `Bearer ${NAS.token}`; if (NAS.csrfToken) headers['X-CSRFToken'] = NAS.csrfToken; headers['Content-Type'] = 'application/json';
                const resp = await fetch('/api/storage/merge', { method: 'POST', headers, body: JSON.stringify({ disk: parent.name, fstype, label }) });
                if (!resp.ok) { const err = await resp.json().catch(() => ({})); addLog(err.error || t('Błąd'), 'error'); showDone(false); return; }
                const reader = resp.body.getReader(); const decoder = new TextDecoder(); let buf = '';
                while (true) { const { done, value } = await reader.read(); if (done) break; buf += decoder.decode(value, { stream: true }); const lines = buf.split('\n'); buf = lines.pop(); for (const line of lines) { if (!line.startsWith('data: ')) continue; try { const ev = JSON.parse(line.slice(6)); if (ev.type === 'step' || ev.type === 'log') addLog(ev.message); if (ev.type === 'done') showDone(ev.success); } catch {} } }
            } catch (e) { addLog(t('Błąd: ') + e.message, 'error'); showDone(false); }
            function showDone(success) {
                resultEl.style.display = ''; const cog = fmtBody.querySelector('.fa-cog.fa-spin');
                resultEl.innerHTML = success
                    ? `<div class="st-fmt-result-ok"><i class="fas fa-check-circle sto-result-ok-icon"></i><div><div class="sto-result-ok-text">${t('Partycje połączone!')}</div></div></div>`
                    : `<div class="st-fmt-result-err"><i class="fas fa-times-circle sto-result-err-icon"></i><div><div class="sto-result-err-text">${t('Łączenie nie powiodło się')}</div></div></div>`;
                if (cog) { cog.classList.remove('fa-cog','fa-spin'); cog.classList.add(success ? 'fa-check-circle' : 'fa-exclamation-triangle'); cog.style.color = success ? '#10b981' : '#ef4444'; }
                fmtFooter.style.display = '';
                fmtFooter.innerHTML = '<button class="btn btn-primary" id="st-merge-done">Zamknij</button>';
                fmtFooter.querySelector('#st-merge-done').onclick = () => { closeFmtModal(); loadDrives(); };
            }
        };
    };

    /* ═══════════════════════════════════════════════════════
       Split / Partition Disk
       ═══════════════════════════════════════════════════════ */
    $('#st-split-btn').onclick = async () => {
        if (!state.selected) return;
        const drive = state.drives.find(d => d.name === state.selected);
        const parentDisk = drive?.parent ? state.drives.find(d => d.name === drive.parent) : (drive?.type === 'disk' ? drive : null);
        if (!parentDisk) { toast('Nie znaleziono dysku', 'warning'); return; }
        if (parentDisk.category === 'system') { toast(t('Nie można partycjonować dysku systemowego!'), 'error'); return; }

        const childParts = state.drives.filter(d => d.parent === parentDisk.name);
        const mountedParts = childParts.filter(d => d.mountpoint);
        if (mountedParts.length) { toast(`Odmontuj najpierw: ${mountedParts.map(d => d.name).join(', ')}`, 'warning'); return; }

        // Calculate disk size in MB
        let diskSizeMB = 0;
        const sizeStr = parentDisk.size || '0';
        const m = sizeStr.match(/([\d.]+)\s*([KMGTP]?)/i);
        if (m) {
            let val = parseFloat(m[1]);
            const unit = (m[2] || '').toUpperCase();
            if (unit === 'T') val *= 1024 * 1024; else if (unit === 'G') val *= 1024; else if (unit === 'M') val *= 1; else if (unit === 'K') val /= 1024;
            diskSizeMB = Math.floor(val);
        }
        if (!diskSizeMB) diskSizeMB = 1024;

        fmtModal.style.display = '';
        fmtBody.innerHTML = '<div class="sto-center-sm"><i class="fas fa-spinner fa-spin sto-spinner"></i></div>';
        fmtFooter.style.display = '';

        let splitFsOptions = [];
        try {
            const data = await api('/storage/format/options');
            splitFsOptions = (data.options || []).filter(o => o.available);
            fmtSystemFs = data.system_fs || 'ext4';
        } catch (e) { fmtBody.innerHTML = `<div class="sto-error-msg">${t('Błąd:')} ${e.message}</div>`; return; }

        const splitParts = [{ size_mb: 0, fstype: fmtSystemFs, label: '' }];

        function renderSplitModal() {
            const usedMB = splitParts.reduce((s, p) => s + (p.size_mb || 0), 0);
            const freeMB = Math.max(0, diskSizeMB - usedMB - 1);
            const fsSelectHtml = (idx, selected) => splitFsOptions.map(fs => `<option value="${fs.value}" ${fs.value === selected ? 'selected' : ''}>${fs.label}${fs.recommended ? ' ★' : ''}</option>`).join('');
            const colors = ['#3b82f6','#10b981','#f59e0b','#ef4444','#8b5cf6','#ec4899','#06b6d4','#84cc16'];

            fmtBody.innerHTML = `
                <div class="st-fmt-drive-info sto-drive-info-split">
                    <i class="fas fa-columns sto-drive-icon-cyan"></i>
                    <div>
                        <div class="sto-bold">/dev/${parentDisk.name} — ${t('Podział na partycje')}</div>
                        <div class="sto-subtitle">${parentDisk.size || '?'} (${diskSizeMB.toLocaleString()} MB) ${parentDisk.model ? '· ' + parentDisk.model : ''}</div>
                    </div>
                </div>
                <div class="st-split-bar">
                    ${splitParts.map((p, i) => {
                        const pctW = p.size_mb ? Math.max(2, (p.size_mb / diskSizeMB) * 100) : Math.max(2, (freeMB / diskSizeMB) * 100);
                        return `<div class="st-split-bar-seg" style="width:${pctW}%;background:${colors[i % colors.length]}" title="Part ${i+1}: ${p.size_mb ? p.size_mb + ' MB' : t('Reszta')}"></div>`;
                    }).join('')}
                </div>
                <div class="st-split-parts" id="st-split-parts">
                    ${splitParts.map((p, i) => `
                    <div class="st-split-part" data-idx="${i}">
                        <div class="st-split-part-num">${i + 1}</div>
                        <div class="st-split-part-fields">
                            <div class="st-split-row">
                                <label>Rozmiar:</label>
                                <input type="number" class="modal-input st-split-size" data-idx="${i}" value="${p.size_mb || ''}" placeholder="Reszta" min="1" max="${diskSizeMB}" style="width:120px">
                                <span class="sto-free-label">MB</span>
                                <label class="storage-check sto-ml-sm"><input type="checkbox" class="st-split-rest" data-idx="${i}" ${!p.size_mb ? 'checked' : ''}> Reszta</label>
                            </div>
                            <div class="st-split-row">
                                <label>FS:</label>
                                <select class="modal-input st-split-fs" data-idx="${i}" style="width:140px">${fsSelectHtml(i, p.fstype)}</select>
                                <label>Etykieta:</label>
                                <input type="text" class="modal-input st-split-label" data-idx="${i}" value="${p.label}" placeholder="opcjonalnie" maxlength="16" style="width:120px">
                                ${splitParts.length > 1 ? `<button class="fm-toolbar-btn btn-red btn-sm st-split-del" data-idx="${i}" title="${t('Usuń')}"><i class="fas fa-times"></i></button>` : ''}
                            </div>
                        </div>
                    </div>`).join('')}
                </div>
                <div class="sto-split-footer">
                    <button class="fm-toolbar-btn btn-green" id="st-split-add" ${splitParts.length >= 8 ? 'disabled' : ''}><i class="fas fa-plus"></i> ${t('Dodaj partycję')}</button>
                    <span class="sto-free-label">Wolne: <b>${freeMB.toLocaleString()}</b> MB</span>
                </div>
                <div class="st-fmt-system-info sto-sys-info-danger sto-mt-md">
                    <i class="fas fa-exclamation-triangle sto-text-danger"></i>
                    <span class="sto-text-danger"><b>${t('UWAGA:')}</b> ${t('Istniejące partycje i dane zostaną usunięte!')}</span>
                </div>`;

            fmtBody.querySelectorAll('.st-split-size').forEach(inp => {
                inp.oninput = () => { splitParts[+inp.dataset.idx].size_mb = parseInt(inp.value) || 0; const cb = fmtBody.querySelector(`.st-split-rest[data-idx="${inp.dataset.idx}"]`); if (cb && inp.value) cb.checked = false; };
            });
            fmtBody.querySelectorAll('.st-split-rest').forEach(cb => {
                cb.onchange = () => {
                    const idx = +cb.dataset.idx;
                    if (cb.checked) { splitParts.forEach((p, i) => { if (i !== idx) p.size_mb = p.size_mb || 1024; }); splitParts[idx].size_mb = 0; renderSplitModal(); }
                    else { splitParts[idx].size_mb = Math.floor(freeMB / 2) || 1024; renderSplitModal(); }
                };
            });
            fmtBody.querySelectorAll('.st-split-fs').forEach(sel => { sel.onchange = () => { splitParts[+sel.dataset.idx].fstype = sel.value; }; });
            fmtBody.querySelectorAll('.st-split-label').forEach(inp => { inp.oninput = () => { splitParts[+inp.dataset.idx].label = inp.value; }; });
            fmtBody.querySelectorAll('.st-split-del').forEach(btn => { btn.onclick = () => { splitParts.splice(+btn.dataset.idx, 1); renderSplitModal(); }; });
            const addBtn = fmtBody.querySelector('#st-split-add');
            if (addBtn) addBtn.onclick = () => {
                if (splitParts.length >= 8) return;
                const hasRest = splitParts.some(p => !p.size_mb);
                splitParts.push({ size_mb: hasRest ? Math.floor(freeMB / 2) || 1024 : 0, fstype: fmtSystemFs, label: '' });
                renderSplitModal();
            };
        }

        renderSplitModal();
        fmtFooter.innerHTML = '<button class="btn" id="st-split-cancel2">Anuluj</button><button class="btn btn-danger" id="st-split-confirm"><i class="fas fa-columns"></i> Podziel i formatuj</button>';
        fmtFooter.querySelector('#st-split-cancel2').onclick = closeFmtModal;

        fmtFooter.querySelector('#st-split-confirm').onclick = async () => {
            fmtBody.querySelectorAll('.st-split-size').forEach(inp => { splitParts[+inp.dataset.idx].size_mb = parseInt(inp.value) || 0; });
            fmtBody.querySelectorAll('.st-split-fs').forEach(sel => { splitParts[+sel.dataset.idx].fstype = sel.value; });
            fmtBody.querySelectorAll('.st-split-label').forEach(inp => { splitParts[+inp.dataset.idx].label = inp.value; });

            const hasRest = splitParts.filter(p => !p.size_mb).length;
            if (hasRest > 1) { toast(t('Tylko jedna partycja może być "Reszta"'), 'warning'); return; }
            const totalMB = splitParts.reduce((s, p) => s + (p.size_mb || 0), 0);
            if (totalMB > diskSizeMB) { toast('Suma przekracza rozmiar dysku!', 'error'); return; }

            const descs = splitParts.map((p, i) => `  ${i+1}. ${p.size_mb ? p.size_mb + ' MB' : t('Reszta')} (${p.fstype})`).join('\n');
            if (!await confirmDialog(`⚠️ ${t('UWAGA!')}\n\n${t('Podzielić')} /dev/${parentDisk.name} ${t('na')} ${splitParts.length} ${t('partycji?')}\n\n${descs}\n\n${t('WSZYSTKIE DANE ZOSTANĄ UTRACONE!')}`)) return;

            fmtFooter.style.display = 'none';
            fmtBody.innerHTML = `<div class="st-fmt-progress"><div class="st-fmt-progress-header"><i class="fas fa-cog fa-spin sto-progress-icon-cyan"></i><div><div class="sto-bold">Partycjonowanie /dev/${parentDisk.name}</div><div class="sto-subtitle">${splitParts.length} partycji</div></div></div><div class="st-fmt-log" id="st-fmt-log"></div><div id="st-fmt-result" style="display:none"></div></div>`;
            const logEl = fmtBody.querySelector('#st-fmt-log');
            const resultEl = fmtBody.querySelector('#st-fmt-result');
            function addLog(msg, type = 'info') {
                const icon = type === 'error' ? 'fa-times-circle' : type === 'success' ? 'fa-check-circle' : 'fa-chevron-right';
                const color = type === 'error' ? '#ef4444' : type === 'success' ? '#10b981' : 'var(--text-muted)';
                logEl.innerHTML += `<div class="st-fmt-log-line"><i class="fas ${icon} sto-log-icon" style="color:${color}"></i><span>${msg}</span></div>`;
                logEl.scrollTop = logEl.scrollHeight;
            }
            try {
                const headers = {}; if (NAS.token) headers['Authorization'] = `Bearer ${NAS.token}`; if (NAS.csrfToken) headers['X-CSRFToken'] = NAS.csrfToken; headers['Content-Type'] = 'application/json';
                const resp = await fetch('/api/storage/partition', { method: 'POST', headers, body: JSON.stringify({ disk: parentDisk.name, partitions: splitParts }) });
                if (!resp.ok) { const err = await resp.json().catch(() => ({})); addLog(err.error || t('Błąd'), 'error'); showDone(false); return; }
                const reader = resp.body.getReader(); const decoder = new TextDecoder(); let buf = '';
                while (true) { const { done, value } = await reader.read(); if (done) break; buf += decoder.decode(value, { stream: true }); const lines = buf.split('\n'); buf = lines.pop(); for (const line of lines) { if (!line.startsWith('data: ')) continue; try { const ev = JSON.parse(line.slice(6)); if (ev.type === 'step' || ev.type === 'log') addLog(ev.message); if (ev.type === 'done') showDone(ev.success); } catch {} } }
            } catch (e) { addLog(t('Błąd: ') + e.message, 'error'); showDone(false); }
            function showDone(success) {
                resultEl.style.display = ''; const cog = fmtBody.querySelector('.fa-cog.fa-spin');
                resultEl.innerHTML = success
                    ? `<div class="st-fmt-result-ok"><i class="fas fa-check-circle sto-result-ok-icon"></i><div><div class="sto-result-ok-text">${t('Partycjonowanie zakończone!')}</div></div></div>`
                    : `<div class="st-fmt-result-err"><i class="fas fa-times-circle sto-result-err-icon"></i><div><div class="sto-result-err-text">${t('Partycjonowanie nie powiodło się')}</div></div></div>`;
                if (cog) { cog.classList.remove('fa-cog','fa-spin'); cog.classList.add(success ? 'fa-check-circle' : 'fa-exclamation-triangle'); cog.style.color = success ? '#10b981' : '#ef4444'; }
                fmtFooter.style.display = '';
                fmtFooter.innerHTML = '<button class="btn btn-primary" id="st-split-done">Zamknij</button>';
                fmtFooter.querySelector('#st-split-done').onclick = () => { closeFmtModal(); loadDrives(); };
            }
        };
    };

    /* ═══════════════════════════════════════════════════════
       LUKS Encryption
       ═══════════════════════════════════════════════════════ */
    $('#st-encrypt-btn').onclick = async () => {
        if (!state.selected) return;
        const drive = state.drives.find(d => d.name === state.selected);
        if (!drive) return;
        const dev = `/dev/${drive.name}`;

        if (drive.fstype === 'crypto_LUKS') {
            // Already encrypted — unlock or lock
            const info = await api(`/encryption/status?device=${encodeURIComponent(dev)}`);
            if (info.unlocked) {
                if (!await confirmDialog(t('Zablokować zaszyfrowany wolumen?'))) return;
                try {
                    const r = await api('/encryption/lock', { method: 'POST', body: { device: dev } });
                    if (r.error) { toast(r.error, 'error'); return; }
                    toast(t('Wolumen zablokowany'), 'success');
                    loadDrives();
                } catch (e) { toast(e.message, 'error'); }
            } else {
                showModal(t('Odblokuj wolumen'), `
                    <div class="usr-form">
                        <label>${t('Hasło szyfrowania')}</label>
                        <input type="password" id="st-luks-pw" class="modal-input" autofocus>
                        <label style="margin-top:8px">${t('Punkt montowania (opcjonalnie)')}</label>
                        <input type="text" id="st-luks-mp" class="modal-input" placeholder="/mnt/encrypted" value="/mnt/encrypted">
                    </div>`, [
                    { label: t('Anuluj'), class: 'secondary' },
                    { label: t('Odblokuj'), class: 'primary', action: async (m) => {
                        const pw = m.querySelector('#st-luks-pw').value;
                        const mp = m.querySelector('#st-luks-mp').value.trim();
                        if (!pw) { toast(t('Podaj hasło'), 'warning'); return; }
                        try {
                            const r = await api('/encryption/unlock', { method: 'POST', body: { device: dev, passphrase: pw, mount: mp } });
                            if (r.error) { toast(r.error, 'error'); return; }
                            toast(t('Wolumen odblokowany'), 'success');
                            loadDrives();
                        } catch (e) { toast(e.message, 'error'); }
                    }}
                ]);
            }
        } else {
            // Not encrypted — encrypt
            if (drive.mountpoint) { toast(t('Odmontuj dysk przed szyfrowaniem'), 'warning'); return; }
            showModal(t('Szyfruj LUKS2 — ') + dev, `
                <div class="usr-form">
                    <div class="st-fmt-system-info sto-sys-info-danger" style="margin-bottom:12px">
                        <i class="fas fa-exclamation-triangle sto-text-danger"></i>
                        <span class="sto-text-danger"><b>${t('UWAGA:')}</b> ${t('Wszystkie dane na urządzeniu zostaną usunięte!')}</span>
                    </div>
                    <label>${t('Hasło szyfrowania (min 8 znaków)')}</label>
                    <input type="password" id="st-luks-pw1" class="modal-input">
                    <label>${t('Powtórz hasło')}</label>
                    <input type="password" id="st-luks-pw2" class="modal-input">
                    <label>${t('Etykieta')}</label>
                    <input type="text" id="st-luks-label" class="modal-input" value="encrypted" maxlength="32">
                    <label>${t('System plików')}</label>
                    <select id="st-luks-fs" class="modal-input">
                        <option value="btrfs">Btrfs</option>
                        <option value="ext4">ext4</option>
                        <option value="xfs">XFS</option>
                    </select>
                    <label style="margin-top:8px"><input type="checkbox" id="st-luks-auto"> ${t('Auto-odblokuj przy starcie (keyfile)')}</label>
                </div>`, [
                { label: t('Anuluj'), class: 'secondary' },
                { label: t('Szyfruj'), class: 'danger', action: async (m) => {
                    const pw1 = m.querySelector('#st-luks-pw1').value;
                    const pw2 = m.querySelector('#st-luks-pw2').value;
                    const label = m.querySelector('#st-luks-label').value.trim() || 'encrypted';
                    const fs = m.querySelector('#st-luks-fs').value;
                    const autoUnlock = m.querySelector('#st-luks-auto').checked;
                    if (!pw1 || pw1.length < 8) { toast(t('Hasło min 8 znaków'), 'warning'); return; }
                    if (pw1 !== pw2) { toast(t('Hasła nie pasują'), 'warning'); return; }
                    try {
                        const r = await api('/encryption/encrypt', { method: 'POST', body: { device: dev, passphrase: pw1, label, filesystem: fs } });
                        if (r.error) { toast(r.error, 'error'); return; }
                        toast(t('Wolumen zaszyfrowany!'), 'success');
                        if (autoUnlock) {
                            await api('/encryption/auto-unlock', { method: 'PUT', body: { device: dev, passphrase: pw1, mount: `/mnt/${label}` } });
                        }
                        loadDrives();
                    } catch (e) { toast(e.message, 'error'); }
                }}
            ]);
        }
    };

    /* ═══════════════════════════════════════════════════════
       SSD Cache Manager
       ═══════════════════════════════════════════════════════ */
    const _cacheBtn = $('#st-cache-btn');
    if (_cacheBtn) _cacheBtn.onclick = async () => {
        const devData = await api('/cache/devices');
        if (devData.error) { toast(devData.error, 'error'); return; }
        const statusData = await api('/cache/status');
        const live = (statusData && statusData.live) || [];
        const configured = (statusData && statusData.configured) || [];

        let activeHtml = '';
        if (configured.length > 0) {
            activeHtml = `<div style="margin-bottom:16px">
                <h4 style="margin:0 0 8px">${t('Aktywne cache')}</h4>
                ${configured.map(c => {
                    const backing = live.flatMap(l => l.backing_devices || []).find(b => b.device === c.backing_device);
                    const hitRate = backing ? (backing.hit_ratio !== null ? backing.hit_ratio + '%' : '—') : '—';
                    const dirty = backing ? (backing.dirty_data || '0') : '0';
                    return `<div class="st-cache-item" style="display:flex;align-items:center;gap:12px;padding:8px;border:1px solid var(--border);border-radius:8px;margin-bottom:6px">
                        <div style="flex:1">
                            <b>${c.cache_device}</b> → <b>${c.backing_device}</b>
                            <span class="sto-action-sub">${c.mode}</span>
                            <span style="margin-left:12px">${t('Trafienia')}: ${hitRate} | ${t('Brudne dane')}: ${dirty}</span>
                        </div>
                        <select class="modal-input st-cache-mode-sel" data-backing="${c.backing_device}" style="width:140px">
                            ${['writethrough','writeback','writearound'].map(m =>
                                `<option value="${m}" ${m === c.mode ? 'selected' : ''}>${m}</option>`
                            ).join('')}
                        </select>
                        <button class="fm-toolbar-btn btn-red btn-sm st-cache-detach" data-backing="${c.backing_device}"><i class="fas fa-unlink"></i> ${t('Odłącz')}</button>
                    </div>`;
                }).join('')}
            </div>`;
        }

        const ssdOpts = (devData.ssds || []).map(s =>
            `<option value="${s.device}">${s.name} — ${s.size} ${s.model}</option>`
        ).join('');
        const hddOpts = (devData.hdds || []).map(h =>
            `<option value="${h.device}">${h.name} — ${h.size} ${h.model}</option>`
        ).join('');

        const hasDevices = ssdOpts && hddOpts;
        const newCacheHtml = hasDevices ? `
            <h4 style="margin:0 0 8px">${t('Nowy cache')}</h4>
            <div class="usr-form">
                <label>${t('Dysk SSD (cache)')}</label>
                <select id="st-cache-ssd" class="modal-input">${ssdOpts}</select>
                <label>${t('Dysk HDD (backing)')}</label>
                <select id="st-cache-hdd" class="modal-input">${hddOpts}</select>
                <label>${t('Tryb cache')}</label>
                <select id="st-cache-mode" class="modal-input">
                    <option value="writethrough">${t('Write-through (bezpieczny)')}</option>
                    <option value="writeback">${t('Write-back (szybszy)')}</option>
                    <option value="writearound">${t('Write-around')}</option>
                </select>
                <div class="st-fmt-system-info sto-sys-info-danger" style="margin-top:8px">
                    <i class="fas fa-exclamation-triangle sto-text-danger"></i>
                    <span class="sto-text-danger"><b>${t('UWAGA:')}</b> ${t('Dane na obu dyskach zostaną usunięte!')}</span>
                </div>
            </div>` : `<p style="color:var(--text-muted)">${t('Brak dostępnych dysków SSD lub HDD do konfiguracji cache.')}</p>`;

        const buttons = [{ label: t('Zamknij'), class: 'secondary' }];
        if (hasDevices) {
            buttons.push({ label: t('Utwórz cache'), class: 'danger', action: async (m) => {
                const ssd = m.querySelector('#st-cache-ssd').value;
                const hdd = m.querySelector('#st-cache-hdd').value;
                const mode = m.querySelector('#st-cache-mode').value;
                if (!ssd || !hdd) { toast(t('Wybierz oba dyski'), 'warning'); return; }
                if (ssd === hdd) { toast(t('SSD i HDD muszą być różnymi dyskami'), 'warning'); return; }
                if (!await confirmDialog(t('Utworzyć SSD cache? Dane na obu dyskach zostaną usunięte!'))) return;
                try {
                    const r = await api('/cache/create', { method: 'POST', body: { cache_device: ssd, backing_device: hdd, mode } });
                    if (r.error) { toast(r.error, 'error'); return; }
                    toast(t('SSD Cache utworzony!'), 'success');
                    loadDrives();
                } catch (e) { toast(e.message, 'error'); }
            }});
        }

        showModal(t('SSD Cache Manager'), activeHtml + newCacheHtml, buttons).then(() => {});

        // Bind detach + mode change after modal renders
        setTimeout(() => {
            document.querySelectorAll('.st-cache-detach').forEach(btn => {
                btn.onclick = async () => {
                    const backing = btn.dataset.backing;
                    if (!await confirmDialog(t('Odłączyć SSD cache od') + ' ' + backing + '?')) return;
                    try {
                        const r = await api('/cache/detach', { method: 'POST', body: { backing_device: backing } });
                        if (r.error) { toast(r.error, 'error'); return; }
                        toast(t('Cache odłączony'), 'success');
                        document.querySelector('.modal-overlay')?.remove();
                        loadDrives();
                    } catch (e) { toast(e.message, 'error'); }
                };
            });
            document.querySelectorAll('.st-cache-mode-sel').forEach(sel => {
                sel.onchange = async () => {
                    const backing = sel.dataset.backing;
                    try {
                        const r = await api('/cache/mode', { method: 'PUT', body: { backing_device: backing, mode: sel.value } });
                        if (r.error) { toast(r.error, 'error'); return; }
                        toast(t('Tryb zmieniony: ') + sel.value, 'success');
                    } catch (e) { toast(e.message, 'error'); }
                };
            });
        }, 100);
    };

    /* ═══════════════════════════════════════════════════════
       Init
       ═══════════════════════════════════════════════════════ */
    $('#st-refresh').onclick = () => loadDrives();
    loadDrives();
    return null;
}

/* ═══════════════════════════════════════════════════════════
   Section: RAID + Volumes (from original raid.js)
   Param defaultTab: 'arrays' or 'lvm'
   ═══════════════════════════════════════════════════════════ */
function _smRaid(el, defaultTab) {
    const body = el;
    const state = {
        tab: 'arrays',
        arrays: [],
        disks: [],
        vgs: [],
        lvs: [],
        pvs: [],
        selectedArray: null,
        pollTimer: null,
    };

    body.innerHTML = `
    <style>
        .raid-wrap { display:flex; flex-direction:column; height:100%; overflow:hidden; }

        /* ── Tabs ── */
        .raid-tabs { display:flex; border-bottom:1px solid var(--border); padding:0 16px; background:var(--bg-secondary); flex-shrink:0; }
        .raid-tab { padding:10px 18px; font-size:12px; font-weight:500; color:var(--text-muted); cursor:pointer; border-bottom:2px solid transparent; transition:color .15s, border-color .15s; white-space:nowrap; display:flex; align-items:center; gap:6px; }
        .raid-tab:hover { color:var(--text-primary); }
        .raid-tab.active { color:var(--accent); border-bottom-color:var(--accent); }

        /* ── Content ── */
        .raid-content { flex:1; overflow-y:auto; padding:16px; }
        .raid-toolbar { display:flex; align-items:center; gap:8px; margin-bottom:14px; flex-wrap:wrap; }
        .raid-toolbar-right { margin-left:auto; display:flex; gap:8px; align-items:center; }
        .raid-status-text { font-size:12px; color:var(--text-muted); }

        /* ── Cards ── */
        .raid-cards { display:grid; grid-template-columns:repeat(auto-fill, minmax(300px, 1fr)); gap:12px; }
        .raid-card { background:var(--bg-card); border-radius:10px; border:1px solid var(--border); padding:16px; cursor:pointer; transition:all .15s; }
        .raid-card:hover { border-color:rgba(79,140,255,.3); background:rgba(79,140,255,.03); }
        .raid-card.selected { border-color:var(--accent)!important; box-shadow:0 0 0 1px var(--accent); }
        .raid-card-head { display:flex; align-items:center; gap:10px; margin-bottom:10px; }
        .raid-card-icon { width:40px; height:40px; border-radius:10px; display:flex; align-items:center; justify-content:center; font-size:18px; flex-shrink:0; }
        .raid-card-title { font-weight:600; font-size:13px; color:var(--text-primary); }
        .raid-card-sub { font-size:11px; color:var(--text-muted); }
        .raid-card-body { font-size:12px; }
        .raid-card-row { display:flex; justify-content:space-between; padding:4px 0; border-bottom:1px solid var(--border); }
        .raid-card-row:last-child { border-bottom:none; }
        .raid-card-label { color:var(--text-muted); }
        .raid-card-val { color:var(--text-primary); font-weight:500; }

        /* ── Badges ── */
        .raid-badge { display:inline-block; padding:2px 8px; border-radius:6px; font-size:10px; font-weight:600; text-transform:uppercase; letter-spacing:.3px; }
        .raid-badge-active { background:rgba(34,197,94,.15); color:#22c55e; }
        .raid-badge-degraded { background:rgba(245,158,11,.15); color:#f59e0b; }
        .raid-badge-rebuilding { background:rgba(59,130,246,.15); color:#3b82f6; }
        .raid-badge-inactive { background:rgba(107,114,128,.15); color:#6b7280; }
        .raid-badge-clean { background:rgba(34,197,94,.15); color:#22c55e; }
        .raid-badge-spare { background:rgba(139,92,246,.15); color:#8b5cf6; }
        .raid-badge-faulty { background:rgba(239,68,68,.15); color:#ef4444; }

        /* ── Progress ── */
        .raid-progress { height:6px; background:var(--bg-primary); border-radius:3px; overflow:hidden; margin-top:6px; }
        .raid-progress-bar { height:100%; background:linear-gradient(90deg, var(--accent), #6366f1); border-radius:3px; transition:width .5s; }
        .raid-progress-label { font-size:10px; color:var(--text-muted); margin-top:4px; }

        /* ── Disk map ── */
        .raid-disk-map { display:flex; flex-wrap:wrap; gap:6px; margin-top:8px; }
        .raid-disk-chip { display:inline-flex; align-items:center; gap:4px; padding:4px 10px; border-radius:6px; font-size:11px; font-weight:500; background:var(--bg-primary); border:1px solid var(--border); }
        .raid-disk-chip .dot { width:8px; height:8px; border-radius:50%; flex-shrink:0; }
        .raid-disk-chip .dot.ok { background:#22c55e; }
        .raid-disk-chip .dot.spare { background:#8b5cf6; }
        .raid-disk-chip .dot.faulty { background:#ef4444; }
        .raid-disk-chip .dot.rebuilding { background:#3b82f6; animation:raidPulse 1.5s infinite; }
        @keyframes raidPulse { 0%,100%{opacity:1} 50%{opacity:.3} }

        /* ── Detail panel ── */
        .raid-detail { background:var(--bg-card); border-radius:10px; border:1px solid var(--border); padding:16px; margin-top:14px; }
        .raid-detail-head { display:flex; align-items:center; gap:10px; margin-bottom:12px; }
        .raid-detail-title { font-weight:600; font-size:14px; color:var(--text-primary); flex:1; }
        .raid-detail-actions { display:flex; gap:6px; }

        .raid-table { width:100%; border-collapse:collapse; font-size:12px; }
        .raid-table th { text-align:left; font-weight:600; padding:8px 10px; border-bottom:2px solid var(--border); color:var(--text-secondary); font-size:11px; text-transform:uppercase; letter-spacing:.3px; }
        .raid-table td { padding:7px 10px; border-bottom:1px solid var(--border); color:var(--text-primary); }
        .raid-table tr:last-child td { border-bottom:none; }

        /* ── Wizard / Forms ── */
        .raid-wizard { background:var(--bg-card); border-radius:10px; border:1px solid var(--border); padding:20px; margin-bottom:14px; }
        .raid-wizard-title { font-weight:600; font-size:14px; color:var(--text-primary); margin-bottom:14px; display:flex; align-items:center; gap:8px; }
        .raid-form-row { display:flex; gap:12px; align-items:center; margin-bottom:10px; flex-wrap:wrap; }
        .raid-form-row label { font-size:12px; color:var(--text-muted); min-width:100px; }
        .raid-form-row .fm-input { flex:1; min-width:120px; }
        .raid-disk-select { display:flex; flex-wrap:wrap; gap:6px; }
        .raid-disk-opt { display:flex; align-items:center; gap:6px; padding:8px 12px; border-radius:8px; background:var(--bg-primary); border:1px solid var(--border); cursor:pointer; font-size:12px; transition:all .15s; }
        .raid-disk-opt:hover { border-color:var(--accent); }
        .raid-disk-opt.checked { border-color:var(--accent); background:rgba(79,140,255,.08); }
        .raid-disk-opt input { accent-color:var(--accent); }
        .raid-disk-opt .dname { font-weight:600; color:var(--text-primary); }
        .raid-disk-opt .dsize { color:var(--text-muted); margin-left:4px; }
        .raid-form-actions { display:flex; gap:8px; margin-top:14px; }
        .raid-level-info { font-size:11px; color:var(--text-muted); padding:6px 10px; background:var(--bg-primary); border-radius:6px; margin-bottom:10px; }

        /* ── LVM ── */
        .raid-lvm-section { margin-bottom:20px; }
        .raid-section-title { font-weight:600; font-size:13px; color:var(--text-primary); margin-bottom:10px; display:flex; align-items:center; gap:8px; }

        /* ── Empty state ── */
        .raid-empty { display:flex; align-items:center; justify-content:center; height:200px; color:var(--text-muted); font-size:14px; flex-direction:column; gap:8px; }
        .raid-empty i { font-size:40px; opacity:.4; }

        /* ── Buttons ── */
        .raid-btn { padding:6px 14px; border-radius:6px; font-size:12px; font-weight:500; cursor:pointer; border:1px solid var(--border); background:var(--bg-primary); color:var(--text-primary); transition:all .15s; }
        .raid-btn:hover { border-color:var(--accent); color:var(--accent); }
        .raid-btn-primary { background:var(--accent); color:#fff; border-color:var(--accent); }
        .raid-btn-primary:hover { opacity:.85; }
        .raid-btn-danger { color:#ef4444; border-color:rgba(239,68,68,.3); }
        .raid-btn-danger:hover { background:rgba(239,68,68,.1); border-color:#ef4444; }
        .raid-btn-sm { padding:4px 10px; font-size:11px; }
        .raid-btn:disabled { opacity:.4; cursor:not-allowed; }
    </style>
    <div class="raid-wrap">
        <div class="raid-tabs" id="raid-tabs">
            <div class="raid-tab active" data-tab="arrays"><i class="fas fa-layer-group"></i> ${t('RAID Arrays')}</div>
            <div class="raid-tab" data-tab="lvm"><i class="fas fa-cubes"></i> ${t('LVM')}</div>
        </div>
        <div class="raid-content" id="raid-content"></div>
    </div>`;

    const $ = s => body.querySelector(s);
    const $$ = s => body.querySelectorAll(s);
    const content = $('#raid-content');

    // Tab switching
    $('#raid-tabs').addEventListener('click', e => {
        const tab = e.target.closest('.raid-tab');
        if (!tab) return;
        $$('.raid-tab').forEach(t => t.classList.remove('active'));
        tab.classList.add('active');
        state.tab = tab.dataset.tab;
        render();
    });

    function render() {
        if (state.tab === 'arrays') renderArrays();
        else if (state.tab === 'lvm') renderLVM();
    }

    // ─── RAID level descriptions ───
    const levelInfo = {
        '0': 'Striping — no redundancy. Best performance, any disk failure loses all data.',
        '1': 'Mirroring — full copy on each disk. Survives N-1 disk failures.',
        '5': 'Striping with parity — survives 1 disk failure. Needs ≥3 disks.',
        '6': 'Double parity — survives 2 disk failures. Needs ≥4 disks.',
        '10': 'Mirrored stripes — good performance + redundancy. Needs ≥4 disks.',
    };
    const minDisks = { '0': 2, '1': 2, '5': 3, '6': 4, '10': 4 };

    // ─── Status helpers ───
    function arrayStateBadge(arr) {
        const s = (arr.state || '').toLowerCase();
        if (s.includes('rebuild') || arr.sync) return `<span class="raid-badge raid-badge-rebuilding">rebuilding</span>`;
        if (s.includes('degrad')) return `<span class="raid-badge raid-badge-degraded">degraded</span>`;
        if (s === 'inactive') return `<span class="raid-badge raid-badge-inactive">inactive</span>`;
        if (s.includes('clean') || s.includes('active')) return `<span class="raid-badge raid-badge-active">active</span>`;
        return `<span class="raid-badge raid-badge-active">${_esc(s || 'active')}</span>`;
    }

    function diskStateDot(st) {
        const s = (st || '').toLowerCase();
        if (s.includes('spare')) return 'spare';
        if (s.includes('faulty') || s.includes('removed')) return 'faulty';
        if (s.includes('rebuild')) return 'rebuilding';
        return 'ok';
    }

    function _esc(s) { const d = document.createElement('div'); d.textContent = s; return d.innerHTML; }

    // ═══════════════════════════════════════════
    //  RAID Arrays tab
    // ═══════════════════════════════════════════
    function renderArrays() {
        content.innerHTML = `
            <div class="raid-toolbar">
                <button class="raid-btn" id="raid-refresh"><i class="fas fa-sync-alt"></i> ${t('Refresh')}</button>
                <button class="raid-btn raid-btn-primary" id="raid-create-btn"><i class="fas fa-plus"></i> ${t('Create Array')}</button>
                <div class="raid-toolbar-right"><span class="raid-status-text" id="raid-status"></span></div>
            </div>
            <div id="raid-wizard-area"></div>
            <div id="raid-cards-area"></div>
            <div id="raid-detail-area"></div>`;

        $('#raid-refresh').onclick = loadArrays;
        $('#raid-create-btn').onclick = showCreateWizard;
        loadArrays();
    }

    async function loadArrays() {
        const statusEl = body.querySelector('#raid-status');
        if (statusEl) statusEl.textContent = t('Loading...');
        try {
            const [arrays, disks] = await Promise.all([
                api('/raid/arrays'),
                api('/raid/disks'),
            ]);
            state.arrays = arrays || [];
            state.disks = disks || [];
            if (statusEl) statusEl.textContent = `${state.arrays.length} ${t('arrays')}, ${state.disks.length} ${t('available disks')}`;
            renderArrayCards();
        } catch (e) {
            if (statusEl) statusEl.textContent = t('Error loading data');
        }
    }

    function renderArrayCards() {
        const area = body.querySelector('#raid-cards-area');
        if (!area) return;
        if (!state.arrays.length) {
            area.innerHTML = `<div class="raid-empty"><i class="fas fa-layer-group"></i><span>${t('No RAID arrays found')}</span></div>`;
            body.querySelector('#raid-detail-area').innerHTML = '';
            return;
        }

        area.innerHTML = `<div class="raid-cards">${state.arrays.map(a => {
            const sel = state.selectedArray === a.name ? 'selected' : '';
            const level = a.level || a.raid_level || '?';
            const size = a.array_size || '';
            const disksArr = a.disks || [];
            const members = a.members || [];
            return `<div class="raid-card ${sel}" data-name="${_esc(a.name)}">
                <div class="raid-card-head">
                    <div class="raid-card-icon" style="background:rgba(79,140,255,.12);color:var(--accent);">
                        <i class="fas fa-layer-group"></i>
                    </div>
                    <div>
                        <div class="raid-card-title">/dev/${_esc(a.name)}</div>
                        <div class="raid-card-sub">RAID ${_esc(level)}</div>
                    </div>
                    <div style="margin-left:auto">${arrayStateBadge(a)}</div>
                </div>
                <div class="raid-card-body">
                    ${size ? `<div class="raid-card-row"><span class="raid-card-label">${t('Size')}</span><span class="raid-card-val">${_esc(size)}</span></div>` : ''}
                    <div class="raid-card-row"><span class="raid-card-label">${t('Disks')}</span><span class="raid-card-val">${disksArr.length || members.length}</span></div>
                    ${a.sync ? `<div style="margin-top:6px">
                        <div class="raid-progress"><div class="raid-progress-bar" style="width:${a.sync.progress}%"></div></div>
                        <div class="raid-progress-label">${_esc(a.sync.action)} — ${a.sync.progress.toFixed(1)}%</div>
                    </div>` : ''}
                </div>
                <div class="raid-disk-map">${(disksArr.length ? disksArr : members.map(m => ({device: '/dev/' + m, state: 'active'}))).map(d => {
                    const dot = diskStateDot(d.state || 'active');
                    const nm = (d.device || d).replace('/dev/', '');
                    return `<span class="raid-disk-chip"><span class="dot ${dot}"></span>${_esc(nm)}</span>`;
                }).join('')}</div>
            </div>`;
        }).join('')}</div>`;

        area.querySelectorAll('.raid-card').forEach(card => {
            card.onclick = () => {
                state.selectedArray = card.dataset.name;
                renderArrayCards();
                renderArrayDetail();
            };
        });

        if (state.selectedArray) renderArrayDetail();
    }

    async function renderArrayDetail() {
        const area = body.querySelector('#raid-detail-area');
        if (!area || !state.selectedArray) { if (area) area.innerHTML = ''; return; }

        area.innerHTML = `<div class="raid-detail"><div class="raid-status-text">${t('Loading...')}</div></div>`;
        try {
            const detail = await api(`/raid/arrays/${encodeURIComponent(state.selectedArray)}/status`);
            const disks = detail.disks || [];
            area.innerHTML = `<div class="raid-detail">
                <div class="raid-detail-head">
                    <i class="fas fa-layer-group" style="color:var(--accent);font-size:16px;"></i>
                    <span class="raid-detail-title">/dev/${_esc(detail.name || state.selectedArray)}</span>
                    <div class="raid-detail-actions">
                        <button class="raid-btn raid-btn-sm" id="raid-add-disk"><i class="fas fa-plus"></i> ${t('Add Disk')}</button>
                        <button class="raid-btn raid-btn-sm raid-btn-danger" id="raid-delete-arr"><i class="fas fa-trash"></i> ${t('Delete')}</button>
                    </div>
                </div>
                <table class="raid-table">
                    <tr><td class="raid-card-label">${t('RAID Level')}</td><td>${_esc(detail.raid_level || '')}</td></tr>
                    <tr><td class="raid-card-label">${t('State')}</td><td>${_esc(detail.state || '')}</td></tr>
                    <tr><td class="raid-card-label">${t('Array Size')}</td><td>${_esc(detail.array_size || '')}</td></tr>
                    <tr><td class="raid-card-label">${t('Active Devices')}</td><td>${_esc(detail.active_devices || '')}</td></tr>
                    <tr><td class="raid-card-label">${t('Failed Devices')}</td><td>${_esc(detail.failed_devices || '')}</td></tr>
                    <tr><td class="raid-card-label">${t('Spare Devices')}</td><td>${_esc(detail.spare_devices || '')}</td></tr>
                    <tr><td class="raid-card-label">${t('UUID')}</td><td style="font-family:monospace;font-size:11px">${_esc(detail.uuid || '')}</td></tr>
                </table>
                ${detail.sync ? `<div style="margin-top:10px">
                    <div class="raid-progress"><div class="raid-progress-bar" style="width:${detail.sync.progress}%"></div></div>
                    <div class="raid-progress-label">${_esc(detail.sync.action)} — ${detail.sync.progress.toFixed(1)}%</div>
                </div>` : ''}
                ${disks.length ? `<div style="margin-top:12px">
                    <div class="raid-section-title"><i class="fas fa-hdd"></i> ${t('Member Disks')}</div>
                    <table class="raid-table">
                        <thead><tr><th>${t('Device')}</th><th>${t('State')}</th><th></th></tr></thead>
                        <tbody>${disks.map(d => `<tr>
                            <td><code>${_esc(d.device)}</code></td>
                            <td><span class="raid-badge raid-badge-${diskStateDot(d.state)}">${_esc(d.state)}</span></td>
                            <td><button class="raid-btn raid-btn-sm raid-btn-danger raid-remove-disk" data-dev="${_esc(d.device)}"><i class="fas fa-eject"></i></button></td>
                        </tr>`).join('')}</tbody>
                    </table>
                </div>` : ''}
            </div>`;

            area.querySelector('#raid-delete-arr').onclick = () => deleteArray(state.selectedArray);
            area.querySelector('#raid-add-disk').onclick = () => showAddDiskDialog(state.selectedArray);
            area.querySelectorAll('.raid-remove-disk').forEach(btn => {
                btn.onclick = () => removeDiskFromArray(state.selectedArray, btn.dataset.dev);
            });
        } catch (e) {
            area.innerHTML = `<div class="raid-detail"><div class="raid-status-text" style="color:#ef4444">${t('Error loading details')}</div></div>`;
        }
    }

    async function deleteArray(name) {
        if (!await confirmDialog(t('Potwierdzenie'), t('Delete array /dev/') + name + '? ' + t('This will destroy the array. Data may be lost!'))) return;
        try {
            await api(`/raid/arrays/${encodeURIComponent(name)}`, { method: 'DELETE' });
            state.selectedArray = null;
            loadArrays();
        } catch (e) {
            toast(t('Failed to delete array'), 'error');
        }
    }

    async function removeDiskFromArray(arrName, device) {
        if (!await confirmDialog(t('Potwierdzenie'), t('Remove ') + device + t(' from /dev/') + arrName + '?')) return;
        try {
            await api(`/raid/arrays/${encodeURIComponent(arrName)}/remove`, {
                method: 'POST', body: { device }
            });
            renderArrayDetail();
            loadArrays();
        } catch (e) {
            toast(t('Failed to remove disk'), 'error');
        }
    }

    function showAddDiskDialog(arrName) {
        const wizard = body.querySelector('#raid-wizard-area');
        if (!wizard) return;
        const available = state.disks;
        if (!available.length) {
            wizard.innerHTML = `<div class="raid-wizard"><div class="raid-status-text">${t('No available disks')}</div></div>`;
            return;
        }
        wizard.innerHTML = `<div class="raid-wizard">
            <div class="raid-wizard-title"><i class="fas fa-plus-circle"></i> ${t('Add Disk to')} /dev/${_esc(arrName)}</div>
            <div class="raid-disk-select" id="raid-add-disk-select">
                ${available.map(d => `<label class="raid-disk-opt">
                    <input type="radio" name="raid-add-dev" value="${_esc(d.device)}">
                    <span class="dname">${_esc(d.name)}</span>
                    <span class="dsize">${_esc(d.size)}</span>
                </label>`).join('')}
            </div>
            <div class="raid-form-actions">
                <button class="raid-btn raid-btn-primary" id="raid-add-confirm">${t('Add')}</button>
                <button class="raid-btn" id="raid-add-cancel">${t('Cancel')}</button>
            </div>
        </div>`;
        wizard.querySelector('#raid-add-cancel').onclick = () => { wizard.innerHTML = ''; };
        wizard.querySelector('#raid-add-confirm').onclick = async () => {
            const sel = wizard.querySelector('input[name="raid-add-dev"]:checked');
            if (!sel) return;
            try {
                await api(`/raid/arrays/${encodeURIComponent(arrName)}/add`, {
                    method: 'POST', body: { device: sel.value }
                });
                wizard.innerHTML = '';
                loadArrays();
                renderArrayDetail();
            } catch (e) {
                toast(t('Failed to add disk'), 'error');
            }
        };
    }

    // ─── Create Wizard ───
    function showCreateWizard() {
        const wizard = body.querySelector('#raid-wizard-area');
        if (!wizard) return;
        const available = state.disks;

        wizard.innerHTML = `<div class="raid-wizard">
            <div class="raid-wizard-title"><i class="fas fa-plus-circle"></i> ${t('Create RAID Array')}</div>

            <div class="raid-form-row">
                <label>${t('RAID Level')}</label>
                <select class="fm-input" id="raid-wiz-level" style="max-width:160px">
                    <option value="1">RAID 1 — Mirror</option>
                    <option value="5">RAID 5 — Parity</option>
                    <option value="6">RAID 6 — Double Parity</option>
                    <option value="0">RAID 0 — Stripe</option>
                    <option value="10">RAID 10 — Mirror+Stripe</option>
                </select>
            </div>
            <div class="raid-level-info" id="raid-wiz-level-info">${levelInfo['1']}</div>

            <div class="raid-form-row">
                <label>${t('Array Name')}</label>
                <input class="fm-input" id="raid-wiz-name" placeholder="${t('auto')}" style="max-width:160px">
            </div>

            <div class="raid-form-row">
                <label>${t('Spare Disks')}</label>
                <input class="fm-input" id="raid-wiz-spares" type="number" min="0" value="0" style="max-width:80px">
            </div>

            <div style="margin-top:8px;margin-bottom:4px;font-size:12px;color:var(--text-muted)">
                <i class="fas fa-hdd"></i> ${t('Select disks')} (${available.length} ${t('available')}):
            </div>
            ${available.length ? `<div class="raid-disk-select" id="raid-wiz-disks">
                ${available.map(d => `<label class="raid-disk-opt">
                    <input type="checkbox" value="${_esc(d.device)}" data-name="${_esc(d.name)}">
                    <span class="dname">${_esc(d.name)}</span>
                    <span class="dsize">${_esc(d.size)}</span>
                    ${d.model ? `<span class="dsize">${_esc(d.model)}</span>` : ''}
                </label>`).join('')}
            </div>` : `<div class="raid-status-text" style="padding:10px 0">${t('No available disks found')}</div>`}

            <div class="raid-form-actions">
                <button class="raid-btn raid-btn-primary" id="raid-wiz-create" ${!available.length ? 'disabled' : ''}><i class="fas fa-check"></i> ${t('Create')}</button>
                <button class="raid-btn" id="raid-wiz-cancel">${t('Cancel')}</button>
            </div>
        </div>`;

        const levelSel = wizard.querySelector('#raid-wiz-level');
        const infoEl = wizard.querySelector('#raid-wiz-level-info');
        levelSel.onchange = () => { infoEl.textContent = levelInfo[levelSel.value] || ''; };

        // Toggle checked class on disk options
        wizard.querySelectorAll('.raid-disk-opt input[type="checkbox"]').forEach(cb => {
            cb.onchange = () => cb.closest('.raid-disk-opt').classList.toggle('checked', cb.checked);
        });

        wizard.querySelector('#raid-wiz-cancel').onclick = () => { wizard.innerHTML = ''; };
        wizard.querySelector('#raid-wiz-create').onclick = async () => {
            const level = levelSel.value;
            const devices = [...wizard.querySelectorAll('#raid-wiz-disks input:checked')].map(cb => cb.value);
            const spares = parseInt(wizard.querySelector('#raid-wiz-spares').value) || 0;
            const name = wizard.querySelector('#raid-wiz-name').value.trim();

            const activeCount = devices.length - spares;
            const needed = minDisks[level] || 2;
            if (activeCount < needed) {
                toast(t('RAID ') + level + t(' requires at least ') + needed + t(' active disks. Selected: ') + activeCount, 'warning');
                return;
            }

            if (!await confirmDialog(t('Potwierdzenie'), t('Create RAID ') + level + t(' with ') + devices.length + t(' disks?') +
                (level === '0' ? '\n⚠️ ' + t('RAID 0 has NO redundancy!') : ''))) return;

            const btn = wizard.querySelector('#raid-wiz-create');
            btn.disabled = true;
            btn.innerHTML = `<i class="fas fa-spinner fa-spin"></i> ${t('Creating...')}`;
            try {
                const payload = { level, devices, spares };
                if (name) payload.name = name;
                const res = await api('/raid/arrays', { method: 'POST', body: payload });
                if (res.error) throw new Error(res.error);
                wizard.innerHTML = '';
                loadArrays();
            } catch (e) {
                toast(t('Failed to create array: ') + (e.message || e), 'error');
                btn.disabled = false;
                btn.innerHTML = `<i class="fas fa-check"></i> ${t('Create')}`;
            }
        };
    }

    // ═══════════════════════════════════════════
    //  LVM tab
    // ═══════════════════════════════════════════
    function renderLVM() {
        content.innerHTML = `
            <div class="raid-toolbar">
                <button class="raid-btn" id="lvm-refresh"><i class="fas fa-sync-alt"></i> ${t('Refresh')}</button>
                <div class="raid-toolbar-right"><span class="raid-status-text" id="lvm-status"></span></div>
            </div>
            <div id="lvm-vg-section"></div>
            <div id="lvm-lv-section"></div>`;

        $('#lvm-refresh').onclick = loadLVM;
        loadLVM();
    }

    async function loadLVM() {
        const statusEl = body.querySelector('#lvm-status');
        if (statusEl) statusEl.textContent = t('Loading...');
        try {
            const [vgs, lvs, disks] = await Promise.all([
                api('/raid/lvm/vgs'),
                api('/raid/lvm/lvs'),
                api('/raid/disks'),
            ]);
            state.vgs = vgs || [];
            state.lvs = lvs || [];
            state.disks = disks || [];
            if (statusEl) statusEl.textContent = `${state.vgs.length} VG, ${state.lvs.length} LV`;
            renderVGSection();
            renderLVSection();
        } catch (e) {
            if (statusEl) statusEl.textContent = t('Error loading data');
        }
    }

    function renderVGSection() {
        const area = body.querySelector('#lvm-vg-section');
        if (!area) return;

        area.innerHTML = `<div class="raid-lvm-section">
            <div class="raid-section-title"><i class="fas fa-archive"></i> ${t('Volume Groups')}
                <button class="raid-btn raid-btn-sm raid-btn-primary" id="lvm-create-vg" style="margin-left:auto"><i class="fas fa-plus"></i> ${t('Create VG')}</button>
            </div>
            <div id="lvm-vg-wizard"></div>
            ${state.vgs.length ? `<div class="raid-cards">${state.vgs.map(vg => `<div class="raid-card">
                <div class="raid-card-head">
                    <div class="raid-card-icon" style="background:rgba(139,92,246,.12);color:#8b5cf6;">
                        <i class="fas fa-archive"></i>
                    </div>
                    <div>
                        <div class="raid-card-title">${_esc(vg.vg_name)}</div>
                        <div class="raid-card-sub">${_esc(vg.vg_size || '')}</div>
                    </div>
                    <button class="raid-btn raid-btn-sm raid-btn-danger lvm-del-vg" data-vg="${_esc(vg.vg_name)}" style="margin-left:auto" title="${t('Delete VG')}"><i class="fas fa-trash"></i></button>
                </div>
                <div class="raid-card-body">
                    <div class="raid-card-row"><span class="raid-card-label">${t('Free')}</span><span class="raid-card-val">${_esc(vg.vg_free || '')}</span></div>
                    <div class="raid-card-row"><span class="raid-card-label">${t('PV Count')}</span><span class="raid-card-val">${_esc(vg.pv_count || '')}</span></div>
                    <div class="raid-card-row"><span class="raid-card-label">${t('LV Count')}</span><span class="raid-card-val">${_esc(vg.lv_count || '')}</span></div>
                </div>
                ${(vg.pvs || []).length ? `<div class="raid-disk-map">${vg.pvs.map(p =>
                    `<span class="raid-disk-chip"><span class="dot ok"></span>${_esc((p.pv_name || '').replace('/dev/', ''))}</span>`
                ).join('')}</div>` : ''}
            </div>`).join('')}</div>` : `<div class="raid-empty"><i class="fas fa-archive"></i><span>${t('No volume groups')}</span></div>`}
        </div>`;

        area.querySelector('#lvm-create-vg').onclick = showCreateVGWizard;
        area.querySelectorAll('.lvm-del-vg').forEach(btn => {
            btn.onclick = async (e) => {
                e.stopPropagation();
                const vgName = btn.dataset.vg;
                if (!await confirmDialog(t('Potwierdzenie'), t('Delete volume group ') + vgName + '?')) return;
                try {
                    const res = await api(`/raid/lvm/vg/${encodeURIComponent(vgName)}`, { method: 'DELETE' });
                    if (res.error) throw new Error(res.error);
                    loadLVM();
                } catch (e) {
                    toast(t('Failed to delete VG: ') + (e.message || e), 'error');
                }
            };
        });
    }

    function showCreateVGWizard() {
        const wizard = body.querySelector('#lvm-vg-wizard');
        if (!wizard) return;
        const available = state.disks;

        wizard.innerHTML = `<div class="raid-wizard">
            <div class="raid-wizard-title"><i class="fas fa-plus-circle"></i> ${t('Create Volume Group')}</div>
            <div class="raid-form-row">
                <label>${t('VG Name')}</label>
                <input class="fm-input" id="lvm-vg-name" placeholder="vg0" style="max-width:200px">
            </div>
            <div style="margin-top:8px;margin-bottom:4px;font-size:12px;color:var(--text-muted)">
                <i class="fas fa-hdd"></i> ${t('Select physical volumes')} (${available.length} ${t('available')}):
            </div>
            ${available.length ? `<div class="raid-disk-select" id="lvm-vg-disks">
                ${available.map(d => `<label class="raid-disk-opt">
                    <input type="checkbox" value="${_esc(d.device)}">
                    <span class="dname">${_esc(d.name)}</span>
                    <span class="dsize">${_esc(d.size)}</span>
                </label>`).join('')}
            </div>` : `<div class="raid-status-text">${t('No available disks')}</div>`}
            <div class="raid-form-actions">
                <button class="raid-btn raid-btn-primary" id="lvm-vg-confirm" ${!available.length ? 'disabled' : ''}>${t('Create')}</button>
                <button class="raid-btn" id="lvm-vg-cancel">${t('Cancel')}</button>
            </div>
        </div>`;

        wizard.querySelectorAll('.raid-disk-opt input[type="checkbox"]').forEach(cb => {
            cb.onchange = () => cb.closest('.raid-disk-opt').classList.toggle('checked', cb.checked);
        });
        wizard.querySelector('#lvm-vg-cancel').onclick = () => { wizard.innerHTML = ''; };
        wizard.querySelector('#lvm-vg-confirm').onclick = async () => {
            const name = wizard.querySelector('#lvm-vg-name').value.trim();
            const devices = [...wizard.querySelectorAll('#lvm-vg-disks input:checked')].map(cb => cb.value);
            if (!name) { toast(t('VG name is required'), 'warning'); return; }
            if (!devices.length) { toast(t('Select at least one device'), 'warning'); return; }
            const btn = wizard.querySelector('#lvm-vg-confirm');
            btn.disabled = true;
            try {
                const res = await api('/raid/lvm/vg', { method: 'POST', body: { name, devices } });
                if (res.error) throw new Error(res.error);
                wizard.innerHTML = '';
                loadLVM();
            } catch (e) {
                toast(t('Failed: ') + (e.message || e), 'error');
                btn.disabled = false;
            }
        };
    }

    function renderLVSection() {
        const area = body.querySelector('#lvm-lv-section');
        if (!area) return;

        area.innerHTML = `<div class="raid-lvm-section">
            <div class="raid-section-title"><i class="fas fa-cube"></i> ${t('Logical Volumes')}
                <button class="raid-btn raid-btn-sm raid-btn-primary" id="lvm-create-lv" style="margin-left:auto" ${!state.vgs.length ? 'disabled' : ''}><i class="fas fa-plus"></i> ${t('Create LV')}</button>
            </div>
            <div id="lvm-lv-wizard"></div>
            ${state.lvs.length ? `<table class="raid-table">
                <thead><tr><th>${t('Name')}</th><th>${t('VG')}</th><th>${t('Size')}</th><th>${t('Path')}</th><th></th></tr></thead>
                <tbody>${state.lvs.map(lv => `<tr>
                    <td><strong>${_esc(lv.lv_name)}</strong></td>
                    <td>${_esc(lv.vg_name)}</td>
                    <td>${_esc(lv.lv_size)}</td>
                    <td><code style="font-size:11px">${_esc(lv.lv_path || `/dev/${lv.vg_name}/${lv.lv_name}`)}</code></td>
                    <td><button class="raid-btn raid-btn-sm raid-btn-danger lvm-del-lv" data-vg="${_esc(lv.vg_name)}" data-lv="${_esc(lv.lv_name)}"><i class="fas fa-trash"></i></button></td>
                </tr>`).join('')}</tbody>
            </table>` : `<div class="raid-empty" style="height:120px"><i class="fas fa-cube"></i><span>${t('No logical volumes')}</span></div>`}
        </div>`;

        area.querySelector('#lvm-create-lv').onclick = showCreateLVWizard;
        area.querySelectorAll('.lvm-del-lv').forEach(btn => {
            btn.onclick = async () => {
                const vg = btn.dataset.vg, lv = btn.dataset.lv;
                if (!await confirmDialog(t('Potwierdzenie'), t('Delete logical volume ') + vg + '/' + lv + '?')) return;
                try {
                    const res = await api(`/raid/lvm/lv/${encodeURIComponent(vg)}/${encodeURIComponent(lv)}`, { method: 'DELETE' });
                    if (res.error) throw new Error(res.error);
                    loadLVM();
                } catch (e) {
                    toast(t('Failed: ') + (e.message || e), 'error');
                }
            };
        });
    }

    function showCreateLVWizard() {
        const wizard = body.querySelector('#lvm-lv-wizard');
        if (!wizard) return;

        wizard.innerHTML = `<div class="raid-wizard">
            <div class="raid-wizard-title"><i class="fas fa-plus-circle"></i> ${t('Create Logical Volume')}</div>
            <div class="raid-form-row">
                <label>${t('Volume Group')}</label>
                <select class="fm-input" id="lvm-lv-vg" style="max-width:200px">
                    ${state.vgs.map(vg => `<option value="${_esc(vg.vg_name)}">${_esc(vg.vg_name)} (${_esc(vg.vg_free || '')} ${t('free')})</option>`).join('')}
                </select>
            </div>
            <div class="raid-form-row">
                <label>${t('LV Name')}</label>
                <input class="fm-input" id="lvm-lv-name" placeholder="lv0" style="max-width:200px">
            </div>
            <div class="raid-form-row">
                <label>${t('Size')}</label>
                <input class="fm-input" id="lvm-lv-size" placeholder="${t('e.g. 10G, 500M')}" style="max-width:160px">
                <label class="raid-disk-opt" style="padding:6px 10px">
                    <input type="checkbox" id="lvm-lv-useall"> <span class="dname">${t('Use all free space')}</span>
                </label>
            </div>
            <div class="raid-form-actions">
                <button class="raid-btn raid-btn-primary" id="lvm-lv-confirm">${t('Create')}</button>
                <button class="raid-btn" id="lvm-lv-cancel">${t('Cancel')}</button>
            </div>
        </div>`;

        const useAllCb = wizard.querySelector('#lvm-lv-useall');
        const sizeInput = wizard.querySelector('#lvm-lv-size');
        useAllCb.onchange = () => { sizeInput.disabled = useAllCb.checked; if (useAllCb.checked) sizeInput.value = ''; };

        wizard.querySelector('#lvm-lv-cancel').onclick = () => { wizard.innerHTML = ''; };
        wizard.querySelector('#lvm-lv-confirm').onclick = async () => {
            const vg_name = wizard.querySelector('#lvm-lv-vg').value;
            const name = wizard.querySelector('#lvm-lv-name').value.trim();
            const size = sizeInput.value.trim();
            const use_all = useAllCb.checked;
            if (!name) { toast(t('LV name is required'), 'warning'); return; }
            if (!use_all && !size) { toast(t('Specify size or use all free space'), 'warning'); return; }
            const btn = wizard.querySelector('#lvm-lv-confirm');
            btn.disabled = true;
            try {
                const res = await api('/raid/lvm/lv', { method: 'POST', body: { vg_name, name, size, use_all } });
                if (res.error) throw new Error(res.error);
                wizard.innerHTML = '';
                loadLVM();
            } catch (e) {
                toast(t('Failed: ') + (e.message || e), 'error');
                btn.disabled = false;
            }
        };
    }

    // ─── Auto-refresh for rebuilds ───
    function startPoll() {
        stopPoll();
        state.pollTimer = setInterval(() => {
            if (state.tab === 'arrays' && state.arrays.some(a => a.sync)) loadArrays();
        }, 10000);
    }
    function stopPoll() { if (state.pollTimer) { clearInterval(state.pollTimer); state.pollTimer = null; } }

    // Initial render
    render();
    startPoll();

    // Cleanup on window close
    const origClose = body.closest('.window')?.querySelector('.win-close');
    if (origClose) {
        const orig = origClose.onclick;
        origClose.onclick = () => { stopPoll(); if (orig) orig(); };
    }

    /* Auto-select the requested tab */
    if (defaultTab === 'lvm') {
        const lvmTab = body.querySelector('[data-tab="lvm"]');
        if (lvmTab) lvmTab.click();
    }
    return () => { stopPoll(); };
}
