/* ═══════════════════════════════════════════════════════════════
   EthOS — Dashboard (system summary)
   ═══════════════════════════════════════════════════════════════ */

AppRegistry['dashboard'] = function (appDef, launchOpts) {
    let refreshTimer = null;
    const w = createWindow('dashboard', {
        title: 'Pulpit',
        icon: 'fa-tachometer-alt',
        iconColor: '#3b82f6',
        width: 880,
        height: 620,
        onClose: () => { if (refreshTimer) clearInterval(refreshTimer); },
    });
    const body = w.body;

    /* ── state ──────────────────────────────────────────────── */
    let S = null;

    /* ── helpers ────────────────────────────────────────────── */
    const fmtBytes = (b) => {
        if (b == null) return '—';
        const units = ['B', 'KB', 'MB', 'GB', 'TB'];
        let i = 0;
        let v = b;
        while (v >= 1024 && i < units.length - 1) { v /= 1024; i++; }
        return v.toFixed(i > 1 ? 1 : 0) + ' ' + units[i];
    };

    const pctColor = (p) => {
        if (p >= 90) return 'var(--danger, #ef4444)';
        if (p >= 70) return 'var(--warning, #f59e0b)';
        return 'var(--accent, #3b82f6)';
    };

    const gauge = (label, pct, sub) => {
        const p = Math.min(100, Math.max(0, pct || 0));
        const r = 40, c = 2 * Math.PI * r;
        const offset = c - (c * p / 100);
        const color = pctColor(p);
        return `
            <div class="dashboard-gauge">
                <svg viewBox="0 0 100 100" class="dashboard-gauge-svg">
                    <circle cx="50" cy="50" r="${r}" fill="none"
                        stroke="var(--overlay-3, #333)" stroke-width="8"/>
                    <circle cx="50" cy="50" r="${r}" fill="none"
                        stroke="${color}" stroke-width="8"
                        stroke-dasharray="${c}" stroke-dashoffset="${offset}"
                        stroke-linecap="round"
                        transform="rotate(-90 50 50)"
                        style="transition:stroke-dashoffset .6s ease"/>
                </svg>
                <div class="dashboard-gauge-value">${Math.round(p)}%</div>
                <div class="dashboard-gauge-label">${label}</div>
                ${sub ? `<div class="dashboard-gauge-sub">${sub}</div>` : ''}
            </div>`;
    };

    const bar = (pct) => {
        const p = Math.min(100, Math.max(0, pct || 0));
        return `<div class="dashboard-bar">
                    <div class="dashboard-bar-fill" style="width:${p}%;background:${pctColor(p)}"></div>
                </div>`;
    };

    const dot = (on) =>
        `<span class="dashboard-dot ${on ? 'dashboard-dot--on' : ''}"></span>`;

    /* ── render ─────────────────────────────────────────────── */
    const render = () => {
        if (!S) { body.innerHTML = '<div class="p-3">Ładowanie…</div>'; return; }

        const temps = (S.temperatures || []).map(t =>
            `<span class="dashboard-temp">${t.label}: ${t.celsius}°C</span>`
        ).join(' ');

        const disksHtml = (S.disks || []).map(d => `
            <div class="dashboard-disk">
                <div class="dashboard-disk-head">
                    <i class="fas fa-hdd"></i>
                    <span class="dashboard-disk-mount">${d.mountpoint}</span>
                    <span class="dashboard-disk-dev">${d.device}</span>
                </div>
                ${bar(d.percent)}
                <div class="dashboard-disk-info">
                    <span>${fmtBytes(d.used)} / ${fmtBytes(d.total)}</span>
                    <span>${d.percent}%</span>
                </div>
            </div>`).join('');

        const netHtml = (S.network || []).map(n => `
            <div class="dashboard-net-row">
                ${dot(n.is_up)}
                <span class="dashboard-net-name">${n.name}</span>
                <span class="dashboard-net-ip">${n.ips.length ? n.ips.join(', ') : '—'}</span>
                ${n.speed ? `<span class="dashboard-net-speed">${n.speed} Mb/s</span>` : ''}
            </div>`).join('');

        let dockerHtml = '';
        if (S.docker) {
            dockerHtml = `
                <div class="dashboard-card">
                    <div class="dashboard-card-title"><i class="fas fa-cubes"></i> Docker</div>
                    <div class="dashboard-docker-stat">
                        <span class="dashboard-big-num">${S.docker.running}</span>
                        <span>/ ${S.docker.total} kontenerów</span>
                    </div>
                </div>`;
        }

        const svc = S.services || {};
        const svcList = [
            ['Samba', 'fa-share-alt', svc.samba],
            ['NFS', 'fa-network-wired', svc.nfs],
            ['Docker', 'fa-cubes', svc.docker],
            ['SSH', 'fa-terminal', svc.ssh],
        ];
        const svcHtml = svcList.map(([name, icon, on]) => `
            <div class="dashboard-svc-row">
                ${dot(on)}
                <i class="fas ${icon}"></i>
                <span>${name}</span>
            </div>`).join('');

        const shares = S.shares || {};

        body.innerHTML = `
            <div class="dashboard-app">

                <!-- System info -->
                <div class="dashboard-card dashboard-card--wide">
                    <div class="dashboard-card-title"><i class="fas fa-server"></i> System</div>
                    <div class="dashboard-sys-grid">
                        <div><span class="dashboard-sys-label">Hostname</span><span class="dashboard-sys-val">${S.hostname}</span></div>
                        <div><span class="dashboard-sys-label">Nazwa</span><span class="dashboard-sys-val">${S.nas_name}</span></div>
                        <div><span class="dashboard-sys-label">Wersja</span><span class="dashboard-sys-val">${S.version}</span></div>
                        <div><span class="dashboard-sys-label">Uptime</span><span class="dashboard-sys-val">${S.uptime}</span></div>
                        ${temps ? `<div><span class="dashboard-sys-label">Temperatura</span><span class="dashboard-sys-val">${temps}</span></div>` : ''}
                    </div>
                </div>

                <!-- Gauges -->
                <div class="dashboard-card dashboard-card--wide">
                    <div class="dashboard-card-title"><i class="fas fa-microchip"></i> Zasoby</div>
                    <div class="dashboard-gauges">
                        ${gauge('CPU', S.cpu.percent, S.cpu.cores + ' rdzeni' + (S.cpu.freq_mhz ? ' · ' + S.cpu.freq_mhz + ' MHz' : ''))}
                        ${gauge('RAM', S.ram.percent, fmtBytes(S.ram.used) + ' / ' + fmtBytes(S.ram.total))}
                        ${gauge('Swap', S.swap.percent, fmtBytes(S.swap.used) + ' / ' + fmtBytes(S.swap.total))}
                    </div>
                </div>

                <!-- Disks -->
                <div class="dashboard-card dashboard-card--wide">
                    <div class="dashboard-card-title"><i class="fas fa-hdd"></i> Dyski</div>
                    <div class="dashboard-disks">${disksHtml || '<div class="dashboard-empty">Brak dysków</div>'}</div>
                </div>

                <!-- Network -->
                <div class="dashboard-card">
                    <div class="dashboard-card-title"><i class="fas fa-ethernet"></i> Sieć</div>
                    <div class="dashboard-net">${netHtml || '<div class="dashboard-empty">—</div>'}</div>
                </div>

                <!-- Docker -->
                ${dockerHtml}

                <!-- Services & shares -->
                <div class="dashboard-card">
                    <div class="dashboard-card-title"><i class="fas fa-cogs"></i> Usługi</div>
                    <div class="dashboard-svc">${svcHtml}</div>
                    <div class="dashboard-shares">
                        <span><i class="fas fa-share-alt"></i> Samba: ${shares.samba || 0}</span>
                        <span><i class="fas fa-network-wired"></i> NFS: ${shares.nfs || 0}</span>
                    </div>
                </div>

            </div>`;
    };

    /* ── fetch ──────────────────────────────────────────────── */
    const load = async () => {
        try {
            S = await api('/dashboard/summary');
            render();
        } catch (e) {
            body.innerHTML = `<div class="p-3" style="color:var(--danger)">Błąd: ${e.message}</div>`;
        }
    };

    /* ── lifecycle ──────────────────────────────────────────── */
    load();
    refreshTimer = setInterval(load, 10000);
};
