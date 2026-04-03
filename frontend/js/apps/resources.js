/* ═══════════════════════════════════════════════════════════
   EthOS — Resource Monitor
   ${t('CPU, RAM, GPU, Dyski, Sieć, Procesy, USB, Docker')}
   ═══════════════════════════════════════════════════════════ */

AppRegistry['resource-monitor'] = function (appDef) {
    createWindow('resource-monitor', {
        title: t('Monitor zasobów'),
        icon: appDef.icon,
        iconColor: appDef.color,
        width: 1100,
        height: 700,
        onRender: (body) => renderResourcesApp(body),
    });
};

function renderResourcesApp(body) {
    // Reboot helper — replaces inline fetch() in onclick attributes
    window._resReboot = function(btn) {
        api('/power/action', { method: 'POST', body: JSON.stringify({ action: 'reboot' }) })
            .then(() => {
                btn.disabled = true;
                btn.innerHTML = '<i class="fas fa-spinner fa-spin"></i> ' + t('Restartowanie…');
            })
            .catch(() => {
                btn.textContent = t('Błąd restartu');
            });
    };

    const sections = ['overview','system','cpu','ram','gpu','disks','smart','network','processes','usb'];
    const sectionLabels = {
        overview: `<i class="fas fa-tachometer-alt"></i> ${t('Przegląd')}`,
        system: `<i class="fas fa-server"></i> ${t('System')}`,
        cpu: '<i class="fas fa-microchip"></i> CPU',
        ram: '<i class="fas fa-memory"></i> RAM',
        gpu: '<i class="fas fa-tv"></i> GPU',
        disks: '<i class="fas fa-hdd"></i> Dyski',
        smart: '<i class="fas fa-heartbeat"></i> S.M.A.R.T.',
        network: `<i class="fas fa-network-wired"></i> ${t('Sieć')}`,
        processes: '<i class="fas fa-list-alt"></i> Procesy',
        usb: '<i class="fas fa-usb"></i> USB',
    };

    body.innerHTML = `
    <div class="res-app">
        <div class="res-sidebar">
            ${sections.map((s,i) => `<div class="res-nav ${i===0?'active':''}" data-section="${s}">${sectionLabels[s]}</div>`).join('')}
            <div class="res-nav-footer" id="res-conn"><i class="fas fa-circle res-conn-dot"></i> ${t('Łączenie...')}</div>
        </div>
        <div class="res-main">
            ${sections.map((s,i) => `<div class="res-section ${i===0?'active':''}" id="res-sec-${s}"></div>`).join('')}
        </div>
    </div>`;

    const $ = (sel) => body.querySelector(sel);
    let currentSection = 'overview';
    let lastData = null;
    let charts = {};
    let cpuHistory = [];
    let ramHistory = [];
    let netHistory = [];
    let hwProfile = null;

    // Nav
    body.querySelectorAll('.res-nav[data-section]').forEach(nav => {
        nav.onclick = () => {
            currentSection = nav.dataset.section;
            body.querySelectorAll('.res-nav').forEach(n => n.classList.remove('active'));
            body.querySelectorAll('.res-section').forEach(s => s.classList.remove('active'));
            nav.classList.add('active');
            $(`#res-sec-${currentSection}`).classList.add('active');
            if (lastData) updateSection(currentSection, lastData);
        };
    });

    function fmt(bytes) {
        if (bytes == null) return '—';
        if (bytes < 1024) return bytes + ' B';
        if (bytes < 1048576) return (bytes/1024).toFixed(1) + ' KB';
        if (bytes < 1073741824) return (bytes/1048576).toFixed(1) + ' MB';
        return (bytes/1073741824).toFixed(2) + ' GB';
    }
    function fmtSpeed(bytes) {
        if (bytes == null) return '0 B/s';
        if (bytes < 1024) return bytes.toFixed(0) + ' B/s';
        if (bytes < 1048576) return (bytes/1024).toFixed(1) + ' KB/s';
        return (bytes/1048576).toFixed(1) + ' MB/s';
    }
    function pct(val) { return val != null ? val.toFixed(1) + '%' : '—'; }

    function bar(percent, color) {
        const p = Math.min(100, Math.max(0, percent || 0));
        return `<div class="res-bar"><div class="res-bar-fill" style="width:${p}%;background:${color || 'var(--accent)'}"></div></div>`;
    }

    function updateAll(data) {
        lastData = data;
        const conn = $('#res-conn');
        conn.innerHTML = '<i class="fas fa-circle res-conn-dot res-conn-online"></i> Online';

        // Save history
        const cpu = data.cpu || {};
        const ram = data.ram || {};
        const net = data.network || {};
        const ts = new Date().toLocaleTimeString('pl', {hour:'2-digit',minute:'2-digit',second:'2-digit'});
        cpuHistory.push({t:ts, v: cpu.usage_percent || 0});
        ramHistory.push({t:ts, v: ram.percent || 0});
        netHistory.push({t:ts, down: net.speed_download || 0, up: net.speed_upload || 0});
        if (cpuHistory.length > 60) cpuHistory.shift();
        if (ramHistory.length > 60) ramHistory.shift();
        if (netHistory.length > 60) netHistory.shift();

        updateSection(currentSection, data);
    }

    function updateSection(section, data) {
        const el = $(`#res-sec-${section}`);
        switch(section) {
            case 'overview': renderOverview(el, data); break;
            case 'system': renderSystem(el); break;
            case 'cpu': renderCPU(el, data); break;
            case 'ram': renderRAM(el, data); break;
            case 'gpu': renderGPU(el, data); break;
            case 'disks': renderDisks(el, data); break;
            case 'smart': renderSMART(el, data); break;
            case 'network': renderNetwork(el, data); break;
            case 'processes': renderProcesses(el, data); break;
            case 'usb': renderUSB(el, data); break;
        }
    }

    function renderOverview(el, data) {
        const cpu = data.cpu || {};
        const ram = data.ram || {};
        const gpu = (data.gpu && data.gpu.length) ? data.gpu[0] : {};
        const net = data.network || {};
        const disks = data.disks || [];
        const procs = (data.processes || []).slice(0, 5);
        const sys = hwProfile ? (hwProfile.system || {}) : {};
        const board = sys.baseboard || {};
        const sysName = [sys.manufacturer, sys.product_name].filter(v => v && v !== 'Default string').join(' ');
        const boardName = [board.manufacturer, board.product_name].filter(v => v && v !== 'Default string' && v !== 'To Be Filled By O.E.M.').join(' ');

        el.innerHTML = `
        ${(sysName || boardName) ? `<div class="res-sys-summary">
            ${sysName ? `<span><i class="fas fa-server"></i> ${sysName}</span>` : ''}
            ${boardName && boardName !== sysName ? `<span><i class="fas fa-th"></i> ${boardName}</span>` : ''}
        </div>` : ''}
        <div class="res-grid-4">
            <div class="res-card">
                <div class="res-card-hdr"><i class="fas fa-microchip"></i> CPU</div>
                <div class="res-big-val">${pct(cpu.usage_percent)}</div>
                ${bar(cpu.usage_percent, '#3b82f6')}
                <div class="res-card-stats">
                    <span>${cpu.name || '—'}</span>
                    <span>${t('Rdzenie:')} ${cpu.physical_cores || cpu.core_count || '—'}</span>
                    <span>${cpu.frequency_current ? (cpu.frequency_current/1000).toFixed(2)+' GHz' : ''}</span>
                    <span>${cpu.temperature != null ? cpu.temperature+'°C' : ''}</span>
                </div>
            </div>
            <div class="res-card">
                <div class="res-card-hdr"><i class="fas fa-memory"></i> RAM</div>
                <div class="res-big-val">${pct(ram.percent)}</div>
                ${bar(ram.percent, '#8b5cf6')}
                <div class="res-card-stats">
                    <span>${t('Użyte:')} ${fmt(ram.used)}</span>
                    <span>${t('Całk.:')} ${fmt(ram.total)}</span>
                </div>
            </div>
            <div class="res-card">
                <div class="res-card-hdr"><i class="fas fa-tv"></i> GPU</div>
                <div class="res-big-val">${gpu.load != null ? pct(gpu.load) : '<span class="res-gpu-nodetect">Nie wykryto</span>'}</div>
                ${bar(gpu.load, '#f59e0b')}
                <div class="res-card-stats">
                    <span>${gpu.name || '<span class="res-gpu-hint">Zainstaluj sterowniki GPU</span>'}</span>
                    <span>${gpu.temperature != null ? gpu.temperature+'°C' : ''}</span>
                </div>
            </div>
            <div class="res-card">
                <div class="res-card-hdr"><i class="fas fa-network-wired"></i> ${t('Sieć')}</div>
                <div class="res-net-speeds">
                    <div><i class="fas fa-arrow-down res-net-dl-icon"></i> ${fmtSpeed(net.speed_download)}</div>
                    <div><i class="fas fa-arrow-up res-net-ul-icon"></i> ${fmtSpeed(net.speed_upload)}</div>
                </div>
                <div class="res-card-stats">
                    <span>↓ ${fmt(net.bytes_recv)}</span>
                    <span>↑ ${fmt(net.bytes_sent)}</span>
                </div>
            </div>
        </div>
        <div class="res-grid-2 res-mt-md">
            <div class="res-card">
                <div class="res-card-hdr"><i class="fas fa-hdd"></i> Dyski</div>
                <div class="res-card-body">${disks.map(d => {
                    const color = d.percent > 90 ? '#ef4444' : d.percent > 70 ? '#eab308' : '#10b981';
                    const isUsb = !!d.is_usb;
                    const isExt = !isUsb && d.mountpoint.startsWith('/media/');
                    const icon = isUsb ? 'fa-usb' : isExt ? 'fa-hdd' : (d.mountpoint === '/' ? 'fa-server' : 'fa-hdd');
                    const icoColor = isUsb ? '#a78bfa' : isExt ? '#f59e0b' : '#3b82f6';
                    const name = d.label || (d.mountpoint === '/' ? 'System' : d.mountpoint.split('/').pop() || d.mountpoint);
                    return `
                    <div class="res-disk-row">
                        <i class="fas ${icon} res-disk-row-icon" style="color:${icoColor}"></i>
                        <span class="res-disk-row-name">${name}</span>
                        <div class="res-disk-row-bar">
                            ${bar(d.percent, color)}
                            <span class="res-disk-row-pct" style="color:${color}">${pct(d.percent)}</span>
                        </div>
                        <span class="res-disk-row-size">${fmt(d.used)} / ${fmt(d.total)}</span>
                    </div>`;
                }).join('') || '<p class="res-text-muted">Brak danych</p>'}</div>
            </div>
            <div class="res-card">
                <div class="res-card-hdr"><i class="fas fa-list-alt"></i> Top procesy (CPU)</div>
                <table class="res-proc-table"><thead><tr><th>PID</th><th>Nazwa</th><th>CPU</th><th>RAM</th></tr></thead>
                <tbody>${procs.map(p => `
                    <tr><td>${p.pid}</td><td>${p.name}</td><td>${pct(p.cpu_percent)}</td><td>${fmt(p.memory_rss)}</td></tr>
                `).join('')}</tbody></table>
            </div>
        </div>`;
    }

    function renderSystem(el) {
        if (!hwProfile) {
            el.innerHTML = `<div class="res-empty"><i class="fas fa-spinner fa-spin"></i> ${t('Ładowanie profilu sprzętowego…')}</div>`;
            return;
        }
        const sys = hwProfile.system || {};
        const cpu = hwProfile.cpu || {};
        const mem = hwProfile.memory || {};
        const bios = sys.bios || {};
        const board = sys.baseboard || {};
        const caches = cpu.caches || {};
        const flags = cpu.notable_flags || [];
        const dimms = mem.dimms || [];

        function infoRow(icon, label, val) {
            if (!val || val === 'Not Specified' || val === 'To Be Filled By O.E.M.' || val === 'Default string') return '';
            return `<div class="res-sys-row"><i class="fas ${icon} res-sys-icon"></i><span class="res-sys-label">${label}</span><span class="res-sys-val">${val}</span></div>`;
        }

        el.innerHTML = `
        <h3><i class="fas fa-server"></i> ${t('Informacje o systemie')}</h3>

        <div class="res-grid-2 res-mt-md">
            <div class="res-card">
                <div class="res-card-hdr"><i class="fas fa-desktop"></i> ${t('System')}</div>
                <div class="res-sys-info">
                    ${infoRow('fa-industry', t('Producent'), sys.manufacturer)}
                    ${infoRow('fa-tag', t('Model'), sys.product_name)}
                    ${infoRow('fa-code-branch', t('Wersja'), sys.version)}
                    ${infoRow('fa-fingerprint', t('Numer seryjny'), sys.serial_number)}
                    ${infoRow('fa-sitemap', t('Rodzina'), sys.family)}
                </div>
            </div>
            <div class="res-card">
                <div class="res-card-hdr"><i class="fas fa-th"></i> ${t('Płyta główna')}</div>
                <div class="res-sys-info">
                    ${infoRow('fa-industry', t('Producent'), board.manufacturer)}
                    ${infoRow('fa-tag', t('Model'), board.product_name)}
                    ${infoRow('fa-code-branch', t('Wersja'), board.version)}
                    ${infoRow('fa-fingerprint', t('Numer seryjny'), board.serial_number)}
                </div>
            </div>
        </div>

        <div class="res-grid-2 res-mt-md">
            <div class="res-card">
                <div class="res-card-hdr"><i class="fas fa-microchip"></i> ${t('Procesor')}</div>
                <div class="res-sys-info">
                    ${infoRow('fa-tag', t('Model'), cpu.model)}
                    ${infoRow('fa-cog', t('Architektura'), cpu.architecture)}
                    ${infoRow('fa-th', t('Rdzenie / Wątki'), cpu.cores ? cpu.cores + ' / ' + (cpu.cores * (cpu.threads_per_core || 1)) : '')}
                    ${infoRow('fa-plug', t('Gniazda'), cpu.sockets)}
                    ${infoRow('fa-tachometer-alt', t('Częstotliwość max'), cpu.freq_max_mhz ? (cpu.freq_max_mhz/1000).toFixed(2)+' GHz' : '')}
                    ${Object.entries(caches).length ? `<div class="res-sys-row"><i class="fas fa-database res-sys-icon"></i><span class="res-sys-label">Cache</span><span class="res-sys-val">${Object.entries(caches).map(([k,v]) => `${k}: ${v}`).join(' · ')}</span></div>` : ''}
                    ${flags.length ? `<div class="res-sys-row"><i class="fas fa-flag res-sys-icon"></i><span class="res-sys-label">${t('Rozszerzenia')}</span><span class="res-sys-val res-hw-flags">${flags.map(f => `<span class="res-hw-flag">${f.toUpperCase()}</span>`).join('')}</span></div>` : ''}
                    ${infoRow('fa-cloud', t('Wirtualizacja'), cpu.virtualization)}
                </div>
            </div>
            <div class="res-card">
                <div class="res-card-hdr"><i class="fas fa-shield-alt"></i> BIOS / UEFI</div>
                <div class="res-sys-info">
                    ${infoRow('fa-industry', t('Producent'), bios.vendor)}
                    ${infoRow('fa-code-branch', t('Wersja'), bios.version)}
                    ${infoRow('fa-calendar', t('Data'), bios.release_date)}
                    ${infoRow('fa-hashtag', t('Rewizja'), bios.bios_revision)}
                </div>
            </div>
        </div>

        ${dimms.length ? `
        <div class="res-card res-mt-md">
            <div class="res-card-hdr"><i class="fas fa-memory"></i> ${t('Moduły pamięci')} (${mem.slots_used || dimms.length}/${mem.slots_total || '?'} ${t('slotów')})</div>
            <table class="res-proc-table">
                <thead><tr><th>${t('Slot')}</th><th>${t('Rozmiar')}</th><th>${t('Typ')}</th><th>${t('Prędkość')}</th><th>${t('Producent')}</th><th>Form Factor</th></tr></thead>
                <tbody>${dimms.map(d => `
                    <tr><td>${d.locator||'—'}</td><td>${d.size||'—'}</td><td>${d.type||'—'}</td><td>${d.speed||'—'}</td><td>${d.manufacturer||'—'}</td><td>${d.form_factor||'—'}</td></tr>
                `).join('')}</tbody>
            </table>
        </div>` : ''}
        `;
    }

    function renderCPU(el, data) {
        const cpu = data.cpu || {};
        const perCore = cpu.per_core || [];
        const hw = hwProfile ? (hwProfile.cpu || {}) : {};
        const caches = hw.caches || {};
        const flags = (hw.notable_flags || []).map(f => f.toUpperCase()).join(', ');
        const arch = hw.architecture || '';
        const virt = hw.virtualization || '';
        const cacheStr = Object.entries(caches).map(([k,v]) => `${k}: ${v}`).join(' · ');

        el.innerHTML = `
        <h3><i class="fas fa-microchip"></i> ${t('Procesor:')} ${cpu.name || '—'}</h3>
        <div class="res-info-bar">
            <span>${t('Rdzenie:')} ${cpu.physical_cores || '—'} (${cpu.core_count || '—'} ${t('wątków')})</span>
            <span>${t('Częstotliwość:')} ${cpu.frequency_current ? (cpu.frequency_current/1000).toFixed(2)+' GHz' : '—'}${cpu.frequency_max ? ' / '+(cpu.frequency_max/1000).toFixed(2)+' GHz max' : ''}</span>
            <span>${t('Temperatura:')} ${cpu.temperature != null ? cpu.temperature+'°C' : '—'}</span>
            <span>${t('Użycie:')} ${pct(cpu.usage_percent)}</span>
        </div>
        ${(arch || cacheStr || flags || virt) ? `
        <div class="res-card res-mt-md">
            <div class="res-card-hdr"><i class="fas fa-info-circle"></i> ${t('Szczegóły sprzętowe')}</div>
            <div class="res-hw-details">
                ${arch ? `<div class="res-hw-row"><span class="res-hw-label">${t('Architektura')}</span><span class="res-hw-val">${arch}</span></div>` : ''}
                ${cacheStr ? `<div class="res-hw-row"><span class="res-hw-label">Cache</span><span class="res-hw-val">${cacheStr}</span></div>` : ''}
                ${flags ? `<div class="res-hw-row"><span class="res-hw-label">${t('Rozszerzenia')}</span><span class="res-hw-val res-hw-flags">${(hw.notable_flags||[]).map(f => `<span class="res-hw-flag">${f.toUpperCase()}</span>`).join('')}</span></div>` : ''}
                ${virt ? `<div class="res-hw-row"><span class="res-hw-label">${t('Wirtualizacja')}</span><span class="res-hw-val">${virt}</span></div>` : ''}
            </div>
        </div>` : ''}
        <div class="res-card res-mt-md">
            <div class="res-card-hdr">${t('Użycie per rdzeń')}</div>
            <div class="res-core-grid">${perCore.map((v,i) => `
                <div class="res-core-item">
                    <span>Core ${i}</span>
                    ${bar(v, '#3b82f6')}
                    <span>${pct(v)}</span>
                </div>`).join('')}</div>
        </div>
        <div class="res-card res-mt-md">
            <div class="res-card-hdr">CPU Top procesy</div>
            <table class="res-proc-table"><thead><tr><th>PID</th><th>Nazwa</th><th>CPU %</th><th>RAM</th><th>Status</th><th>${t('Użytkownik')}</th></tr></thead>
            <tbody>${(data.processes||[]).sort((a,b)=>(b.cpu_percent||0)-(a.cpu_percent||0)).slice(0,10).map(p => `
                <tr><td>${p.pid}</td><td>${p.name}</td><td>${pct(p.cpu_percent)}</td><td>${fmt(p.memory_rss)}</td><td>${p.status||''}</td><td>${p.username||''}</td></tr>
            `).join('')}</tbody></table>
        </div>`;
    }

    function renderRAM(el, data) {
        const ram = data.ram || {};
        el.innerHTML = `
        <h3><i class="fas fa-memory"></i> ${t('Pamięć RAM')}</h3>
        <div class="res-info-bar">
            <span>${t('Całkowita:')} ${fmt(ram.total)}</span>
            <span>${t('Użyta:')} ${fmt(ram.used)} (${pct(ram.percent)})</span>
            <span>${t('Dostępna:')} ${fmt(ram.available)}</span>
            <span>Cache: ${fmt(ram.cached)}</span>
            <span>Swap: ${fmt(ram.swap_used)} / ${fmt(ram.swap_total)}</span>
        </div>
        <div class="res-card res-mt-md">
            ${bar(ram.percent, '#8b5cf6')}
            <div class="res-ram-center">${pct(ram.percent)}</div>
        </div>
        <div class="res-card res-mt-md">
            <div class="res-card-hdr">RAM Top procesy</div>
            <table class="res-proc-table"><thead><tr><th>PID</th><th>Nazwa</th><th>RAM %</th><th>RAM</th><th>CPU %</th><th>Status</th></tr></thead>
            <tbody>${(data.processes||[]).sort((a,b)=>(b.memory_percent||0)-(a.memory_percent||0)).slice(0,10).map(p => `
                <tr><td>${p.pid}</td><td>${p.name}</td><td>${pct(p.memory_percent)}</td><td>${fmt(p.memory_rss)}</td><td>${pct(p.cpu_percent)}</td><td>${p.status||''}</td></tr>
            `).join('')}</tbody></table>
        </div>`;
    }

    function renderGPU(el, data) {
        // Don't overwrite the GPU section while install is in progress or waiting for reboot
        if (_gpuInstalling) return;

        const gpus = data.gpu || [];
        if (!gpus.length) {
            // Check if we already have detected hardware cached
            if (!el._gpuDetected) {
                el.innerHTML = `<div class="res-empty">
                    <i class="fas fa-tv res-empty-icon"></i>
                    <p class="res-empty-title">Brak wykrytego GPU</p>
                    <p class="res-empty-detect">${t('Wykrywanie sprzętu…')}</p>
                </div>`;
                fetch('/api/resources/gpu/detect', {headers:{'Authorization':'Bearer '+NAS.token,'X-CSRFToken':NAS.csrfToken}}).then(r => r.json()).then(hw => {
                    el._gpuDetected = hw;
                    _renderGpuNoDriver(el, hw);
                }).catch(() => {
                    _renderGpuNoDriver(el, {cards: []});
                });
            } else {
                _renderGpuNoDriver(el, el._gpuDetected);
            }
            return;
        }
        el._gpuDetected = null;
        el.innerHTML = gpus.map(g => `
        <div class="res-card">
            <div class="res-card-hdr"><i class="fas fa-tv"></i> ${g.name || 'GPU'}</div>
            <div class="res-grid-2">
                <div>
                    <span>${t('Obciążenie:')}</span>
                    ${bar(g.load, '#f59e0b')}
                    <span class="res-big-val">${pct(g.load)}</span>
                </div>
                <div>
                    <span>VRAM: ${fmt(g.memory_used)} / ${fmt(g.memory_total)}</span>
                    ${bar(g.memory_total ? ((g.memory_used||0)/g.memory_total*100) : 0, '#ef4444')}
                </div>
            </div>
            <div class="res-info-bar res-mt-sm">
                <span>Temperatura: ${g.temperature != null ? g.temperature+'°C':'—'}</span>
                <span>Driver: ${g.driver || '—'}</span>
            </div>
        </div>`).join('');
    }

    let _gpuInstallListener = null;
    let _gpuInstalling = false;  // true while install is running or waiting for reboot

    function _renderGpuNoDriver(el, hw) {
        const cards = hw.cards || [];
        const vendorIcons = {nvidia: 'fa-bolt', amd: 'fa-fire', intel: 'fa-microchip', unknown: 'fa-question-circle'};
        const vendorColors = {nvidia: '#76b900', amd: '#ed1c24', intel: '#0071c5', unknown: '#888'};

        if (!cards.length) {
            el.innerHTML = `<div class="res-empty">
                <i class="fas fa-tv res-empty-icon"></i>
                <p class="res-empty-title">Brak wykrytego GPU</p>
                <p class="res-empty-desc">
                    Nie wykryto karty graficznej w systemie.
                </p>
            </div>`;
            return;
        }

        el.innerHTML = cards.map((c, i) => `
        <div class="res-card res-mb-md">
            <div class="res-card-hdr">
                <i class="fas ${vendorIcons[c.vendor]||vendorIcons.unknown}" style="color:${vendorColors[c.vendor]||vendorColors.unknown}"></i>
                ${c.name || 'Karta graficzna'}
            </div>
            <div class="res-gpu-card-body">
                <div class="res-flex-row-mb">
                    <span class="res-label">Producent:</span>
                    <span class="res-vendor-name">${c.vendor}</span>
                </div>
                <div class="res-flex-row-mb">
                    <span class="res-label">Pakiety:</span>
                    <span class="res-mono-sm">${(c.packages||[]).join(', ') || '—'}</span>
                </div>
                ${c.driver_installed ? `
                <div class="res-gpu-status-ok">
                    <i class="fas fa-check-circle res-gpu-status-ok-icon"></i>
                    <span class="res-gpu-status-ok-text">${t('Sterowniki zainstalowane')}</span>
                </div>` : (c.packages_installed && c.reboot_required) ? `
                <div class="res-gpu-status-warn">
                    <i class="fas fa-exclamation-triangle res-gpu-status-warn-icon"></i>
                    <span class="res-gpu-status-warn-text">${t('Sterowniki zainstalowane, wymagany restart systemu')}</span>
                </div>
                <div class="res-reboot-area">
                    <button onclick="window._resReboot(this)"
                        class="res-reboot-btn">
                        <i class="fas fa-redo"></i> ${t('Uruchom ponownie')}
                    </button>
                </div>` : c.install_cmd ? `
                <div id="gpu-install-area-${i}" class="res-mt-md">
                    <button onclick="window._gpuInstallDriver(${i})"
                        class="res-gpu-install-btn res-gpu-install-btn-base"
                        style="background:${vendorColors[c.vendor]||'#3b82f6'}">
                        <i class="fas fa-download"></i> ${t('Pobierz i zainstaluj sterowniki')}
                    </button>
                </div>` : `<p class="res-no-install">${t('Brak automatycznej instalacji dla tego producenta.')}</p>`}
            </div>
        </div>`).join('');

        // Bind install handler
        window._gpuInstallDriver = (cardIndex) => {
            const area = el.querySelector('#gpu-install-area-' + cardIndex);
            if (!area) return;
            _gpuInstalling = true;
            area.innerHTML = `
                <div class="res-install-wrap">
                    <div class="res-install-status-row">
                        <i class="fas fa-spinner fa-spin res-install-spinner"></i>
                        <span id="gpu-install-status" class="res-install-status">${t('Rozpoczynanie…')}</span>
                    </div>
                    <div class="res-install-bar-bg">
                        <div id="gpu-install-bar" class="res-install-bar-fill"></div>
                        <span id="gpu-install-pct" class="res-install-bar-pct">0%</span>
                    </div>
                    <div id="gpu-install-detail" class="res-install-detail"></div>
                </div>`;

            // Register socketio listener
            if (_gpuInstallListener && NAS.socket) NAS.socket.off('gpu_driver_progress', _gpuInstallListener);
            _gpuInstallListener = (ev) => {
                const barEl = document.getElementById('gpu-install-bar');
                const pctEl = document.getElementById('gpu-install-pct');
                const statusEl = document.getElementById('gpu-install-status');
                const detailEl = document.getElementById('gpu-install-detail');
                if (barEl) barEl.style.width = ev.progress + '%';
                if (pctEl) pctEl.textContent = ev.progress + '%';
                if (statusEl) statusEl.textContent = ev.message || '';
                if (detailEl && ev.detail) detailEl.textContent = ev.detail;

                if (ev.phase === 'done') {
                    if (statusEl) statusEl.innerHTML = '<span class="res-status-success"><i class="fas fa-check-circle"></i> ' + ev.message + '</span>';
                    if (barEl) barEl.style.background = 'linear-gradient(90deg,#22c55e,#16a34a)';
                    // Clear cached detection so next render re-fetches fresh state
                    el._gpuDetected = null;
                    if (ev.reboot_required) {
                        const rebootArea = document.createElement('div');
                        rebootArea.style.cssText = 'margin-top:10px';
                        rebootArea.innerHTML = `<button onclick="window._resReboot(this)"
                            class="res-reboot-btn">
                            <i class="fas fa-redo"></i> Uruchom ponownie
                        </button>`;
                        area.appendChild(rebootArea);
                    }
                } else if (ev.phase === 'error') {
                    if (statusEl) statusEl.innerHTML = '<span class="res-status-error"><i class="fas fa-exclamation-triangle"></i> ' + ev.message + '</span>';
                    if (barEl) barEl.style.background = '#ef4444';
                    _gpuInstalling = false;
                }
            };
            if (NAS.socket) NAS.socket.on('gpu_driver_progress', _gpuInstallListener);

            // Trigger install
            api('/resources/gpu/install', {
                method: 'POST',
                body: JSON.stringify({card_index: cardIndex})
            }).then(res => {
                if (res.error) {
                    const statusEl = document.getElementById('gpu-install-status');
                    if (statusEl) statusEl.innerHTML = '<span class="res-status-error">' + res.error + '</span>';
                }
            }).catch(err => {
                const statusEl = document.getElementById('gpu-install-status');
                if (statusEl) statusEl.innerHTML = `<span class="res-status-error">${t('Błąd połączenia')}</span>`;
            });
        };
    }

    function renderDisks(el, data) {
        const disks = data.disks || [];

        function diskCat(d) {
            if (d.is_usb) return 'usb';
            if (d.mountpoint.startsWith('/media/')) return 'ext';
            return 'sys';
        }
        function diskIcon(d) {
            const c = diskCat(d);
            if (c === 'usb') return 'fa-usb';
            if (c === 'ext') return 'fa-hdd';
            return d.mountpoint === '/' ? 'fa-server' : 'fa-hdd';
        }
        function diskIconColor(d) {
            const c = diskCat(d);
            if (c === 'usb') return '#a78bfa';
            if (c === 'ext') return '#f59e0b';
            return '#3b82f6';
        }
        function diskLabel(d) {
            if (d.label) return d.label;
            if (d.mountpoint === '/') return 'System /';
            const parts = d.mountpoint.split('/');
            return parts[parts.length - 1] || d.mountpoint;
        }
        function diskColor(p) { return p > 90 ? '#ef4444' : p > 70 ? '#eab308' : '#10b981'; }

        function renderCard(d) {
            const color = diskColor(d.percent);
            const modelLine = d.model ? `<span class="res-disk-model">${d.model}</span>` : '';
            return `
            <div class="ddisk-card">
                <div class="ddisk-icon" style="color:${diskIconColor(d)}"><i class="fas ${diskIcon(d)}"></i></div>
                <div class="ddisk-info">
                    <div class="ddisk-name">${diskLabel(d)} ${modelLine}</div>
                    <div class="ddisk-meta">${d.device} · ${d.fstype || '?'} · ${d.mountpoint}</div>
                    <div class="ddisk-bar-wrap">
                        <div class="ddisk-bar"><div class="ddisk-bar-fill" style="width:${d.percent}%;background:${color}"></div></div>
                        <span class="ddisk-pct" style="color:${color}">${pct(d.percent)}</span>
                    </div>
                    <div class="ddisk-sizes">
                        <span><b>${fmt(d.used)}</b> ${t('zajęte')}</span>
                        <span><b>${fmt(d.free)}</b> wolne</span>
                        <span>z <b>${fmt(d.total)}</b></span>
                    </div>
                </div>
            </div>`;
        }

        function renderGroup(title, icon, list) {
            if (!list.length) return '';
            return `<div class="ddisk-group"><div class="ddisk-group-title"><i class="fas ${icon}"></i> ${title}</div>
                <div class="ddisk-group-cards">${list.map(renderCard).join('')}</div></div>`;
        }

        const sys = disks.filter(d => diskCat(d) === 'sys');
        const ext = disks.filter(d => diskCat(d) === 'ext');
        const usb = disks.filter(d => diskCat(d) === 'usb');

        el.innerHTML = `<h3><i class="fas fa-hdd"></i> ${t('Dyski i pamięć masowa')}</h3>` +
            renderGroup('Dyski systemowe', 'fa-server', sys) +
            renderGroup(t('Dyski zewnętrzne'), 'fa-hdd', ext) +
            renderGroup('Dyski USB', 'fa-usb', usb) +
            (disks.length === 0 ? `<div class="res-empty"><p>${t('Brak dysków')}</p></div>` : '');
    }

    function renderSMART(el, data) {
        const smart = data.smart || [];
        
        if (!smart.length) {
            el.innerHTML = `<div class="res-empty">
                <i class="fas fa-heartbeat res-empty-icon"></i>
                <p class="res-empty-title">Brak danych S.M.A.R.T.</p>
                <p class="res-empty-desc">${t('Może być wymagana konfiguracja smartmontools lub brak obsługiwanych dysków.')}</p>
            </div>`;
            return;
        }

        el.innerHTML = `<h3><i class="fas fa-heartbeat"></i> ${t('Zdrowie dysków (S.M.A.R.T.)')}</h3>` + smart.map(d => {
            const healthColor = d.health === 'PASS' ? '#10b981' : '#ef4444';
            const healthIcon = d.health === 'PASS' ? 'fa-check-circle' : 'fa-exclamation-triangle';
            const tempColor = (d.temperature > 50) ? '#ef4444' : (d.temperature > 45 ? '#eab308' : '#fff');
            
            return `
            <div class="res-card res-mb-md">
                <div class="res-card-hdr">
                    <div>
                        <i class="fas fa-hdd" style="color:#a78bfa"></i> ${d.model} 
                        <span class="res-mono-sm" style="opacity:0.7">(${d.device})</span>
                    </div>
                    <div style="margin-left:auto;color:${healthColor};font-weight:bold">
                        <i class="fas ${healthIcon}"></i> ${d.health}
                    </div>
                </div>
                <div style="display:grid;grid-template-columns:1fr 1fr;gap:10px;padding:15px 0;">
                    <div class="res-kv-row">
                        <span class="res-label">Serial Number:</span>
                        <span class="res-val">${d.serial}</span>
                    </div>
                    <div class="res-kv-row">
                        <span class="res-label">Temperatura:</span>
                        <span class="res-val" style="color:${tempColor}">${d.temperature ? d.temperature + '°C' : '—'}</span>
                    </div>
                    <div class="res-kv-row">
                        <span class="res-label">Power On Hours:</span>
                        <span class="res-val">${d.power_on_hours} h</span>
                    </div>
                    <div class="res-kv-row">
                        <span class="res-label">${t('Żywotność (SSD):')}</span>
                        <span class="res-val">${d.remaining_life >= 0 ? pct(d.remaining_life) : '—'}</span>
                    </div>
                </div>
                
                <div class="res-sep" style="border-top:1px solid rgba(255,255,255,0.1);margin:10px 0;"></div>
                
                <div style="display:grid;grid-template-columns:repeat(3,1fr);gap:10px;text-align:center;">
                    <div style="background:rgba(255,255,255,0.05);padding:10px;border-radius:6px;${d.reallocated_sectors > 0 ? 'border:1px solid #ef4444' : ''}">
                        <div style="font-size:11px;opacity:0.7;margin-bottom:4px">Reallocated Sectors</div>
                        <div style="font-size:16px;font-weight:bold;color:${d.reallocated_sectors > 0 ? '#ef4444' : '#fff'}">${d.reallocated_sectors}</div>
                    </div>
                    <div style="background:rgba(255,255,255,0.05);padding:10px;border-radius:6px;${d.pending_sectors > 0 ? 'border:1px solid #ef4444' : ''}">
                        <div style="font-size:11px;opacity:0.7;margin-bottom:4px">Pending Sectors</div>
                        <div style="font-size:16px;font-weight:bold;color:${d.pending_sectors > 0 ? '#ef4444' : '#fff'}">${d.pending_sectors}</div>
                    </div>
                    <div style="background:rgba(255,255,255,0.05);padding:10px;border-radius:6px;">
                        <div style="font-size:11px;opacity:0.7;margin-bottom:4px">UDMA CRC Errors</div>
                        <div style="font-size:16px;font-weight:bold">${d.udma_crc_errors}</div>
                    </div>
                </div>
            </div>`;
        }).join('');
    }

    function renderNetwork(el, data) {
        const net = data.network || {};
        const ifaces = net.interfaces || [];
        el.innerHTML = `
        <h3><i class="fas fa-network-wired"></i> ${t('Sieć')}</h3>
        <div class="res-grid-2">
            <div class="res-card">
                <div class="res-card-hdr"><i class="fas fa-arrow-down res-net-dl-icon"></i> Download</div>
                <div class="res-big-val">${fmtSpeed(net.speed_download)}</div>
                <span class="res-text-muted">${t('Łącznie:')} ${fmt(net.bytes_recv)}</span>
            </div>
            <div class="res-card">
                <div class="res-card-hdr"><i class="fas fa-arrow-up res-net-ul-icon"></i> Upload</div>
                <div class="res-big-val">${fmtSpeed(net.speed_upload)}</div>
                <span class="res-text-muted">${t('Łącznie:')} ${fmt(net.bytes_sent)}</span>
            </div>
        </div>
        ${ifaces.length ? `
        <div class="res-card res-mt-md">
            <div class="res-card-hdr">Interfejsy</div>
            <table class="res-proc-table"><thead><tr><th>Interfejs</th><th>IP</th><th>↓ Speed</th><th>↑ Speed</th></tr></thead>
            <tbody>${ifaces.map(i => `
                <tr><td>${i.name}</td><td>${i.addresses ? i.addresses.join(', ') : '—'}</td>
                <td>${fmtSpeed(i.speed_download)}</td><td>${fmtSpeed(i.speed_upload)}</td></tr>
            `).join('')}</tbody></table>
        </div>` : ''}`;
    }

    function renderProcesses(el, data) {
        const procs = data.processes || [];
        // Keep search state
        let search = '';
        const existingInput = el.querySelector('#res-proc-search');
        if (existingInput) search = existingInput.value;

        const filtered = search ? procs.filter(p => p.name.toLowerCase().includes(search.toLowerCase())) : procs;

        el.innerHTML = `
        <h3><i class="fas fa-list-alt"></i> Procesy (${procs.length})</h3>
        <div class="res-proc-controls">
            <input type="text" id="res-proc-search" class="fm-input res-proc-search" placeholder="Szukaj procesu..." value="${search}">
        </div>
        <div class="res-card res-proc-card">
            <table class="res-proc-table"><thead><tr>
                <th>PID</th><th>Nazwa</th><th>CPU %</th><th>RAM %</th><th>RAM</th><th>Status</th><th>${t('Użytkownik')}</th><th>Akcja</th>
            </tr></thead>
            <tbody>${filtered.slice(0, 100).map(p => `
                <tr><td>${p.pid}</td><td title="${p.cmdline||''}">${p.name}</td><td>${pct(p.cpu_percent)}</td>
                <td>${pct(p.memory_percent)}</td><td>${fmt(p.memory_rss)}</td>
                <td>${p.status||''}</td><td>${p.username||''}</td>
                <td><button class="fm-toolbar-btn btn-red btn-sm" data-kill="${p.pid}" data-name="${p.name}"><i class="fas fa-times"></i></button></td></tr>
            `).join('')}</tbody></table>
        </div>`;

        el.querySelector('#res-proc-search').oninput = (e) => {
            search = e.target.value;
            renderProcesses(el, data);
        };

        el.querySelectorAll('button[data-kill]').forEach(btn => {
            btn.onclick = async () => {
                if (!await confirmDialog(t('Zakończyć proces') + ` ${btn.dataset.name} (PID: ${btn.dataset.kill})?`)) return;
                try {
                    await api(`/resources/kill/${btn.dataset.kill}`, {method:'POST'});
                    toast(t('Proces zakończony'), 'success');
                } catch(e) { toast(t('Błąd'), 'error'); }
            };
        });
    }

    function renderUSB(el, data) {
        const devices = data.usb || [];

        const classIcons = {
            '03': 'fa-keyboard', '07': 'fa-print', '08': 'fa-hdd',
            '09': 'fa-sitemap', '0e': 'fa-video', 'e0': 'fa-broadcast-tower',
            '01': 'fa-music', '06': 'fa-camera',
        };

        el.innerHTML = `<h3><i class="fas fa-usb"></i> ${t('Urządzenia USB')} (${devices.length})</h3>` +
            (devices.length ? devices.map(d => {
                const icon = classIcons[d.device_class] || 'fa-usb';
                const name = d.product || t('Nieznane urządzenie');
                const mfr = d.manufacturer || '';
                const cls = d.device_class_name || '';
                const speed = d.speed ? d.speed + ' Mbit/s' : '';
                const power = d.power || '';
                return `
            <div class="res-card res-mt-sm">
                <div class="res-card-hdr res-usb-hdr">
                    <i class="fas ${icon} res-usb-icon"></i>
                    <span>${name}</span>
                    ${cls ? `<span class="res-usb-badge">${cls}</span>` : ''}
                </div>
                <div class="res-info-bar res-usb-info">
                    ${mfr ? `<span><i class="fas fa-industry res-icon-muted"></i>${mfr}</span>` : ''}
                    <span><i class="fas fa-barcode res-icon-muted"></i>${d.vendor_id}:${d.product_id}</span>
                    <span><i class="fas fa-plug res-icon-muted"></i>Bus ${d.bus}, Dev ${d.device}</span>
                    ${speed ? `<span><i class="fas fa-tachometer-alt res-icon-muted"></i>${speed}</span>` : ''}
                    ${power ? `<span><i class="fas fa-bolt res-icon-muted"></i>${power}</span>` : ''}
                    ${d.serial ? `<span><i class="fas fa-fingerprint res-icon-muted"></i>${d.serial.substring(0, 20)}</span>` : ''}
                </div>
            </div>`;
            }).join('') : `<div class="res-empty"><p>${t('Brak urządzeń USB')}</p></div>`);
    }

    // Listen on socketio for resources_update
    if (NAS.socket) {
        const handler = (data) => { updateAll(data); };
        NAS.socket.on('resources_update', handler);
        // Clean up on window close
        const win = body.closest('.window');
        if (win) {
            const orig = win._onClose;
            win._onClose = () => { NAS.socket.off('resources_update', handler); if (orig) orig(); };
        }
    }

    // Initial load
    (async () => {
        try {
            const [d, hw] = await Promise.all([
                api('/resources/all'),
                api('/hardware/profile')
            ]);
            hwProfile = hw;
            updateAll(d);
        } catch(e) {
            $(`#res-sec-overview`).innerHTML = `<div class="res-empty"><p>${t('Błąd ładowania danych')}</p></div>`;
        }
    })();
}
