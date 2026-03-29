/* ═══════════════════════════════════════════════════════════
   EthOS — Storage Manager  (card-based UI)
   ${t('Zarządzanie dyskami: montowanie, formatowanie, partycje')}
   ═══════════════════════════════════════════════════════════ */

AppRegistry['storage-manager'] = function (appDef) {
    createWindow('storage-manager', {
        title: t('Menedżer dysków'),
        icon: appDef.icon,
        iconColor: appDef.color,
        width: 920,
        height: 620,
        onRender: (body) => renderStorageApp(body),
    });
};

function renderStorageApp(body) {
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
            const info = await api(`/storage/smart?disk=${encodeURIComponent(diskName)}`);
            if (!info.available) {
                smartBody.innerHTML = `<div class="sto-info-msg"><i class="fas fa-info-circle sto-icon-lg"></i><div class="sto-mt-sm">${t('SMART niedostępny dla tego dysku')}</div></div>`;
                return;
            }
            const hColor = info.health === 'PASSED' ? '#10b981' : '#ef4444';
            const tColor = (info.temperature || 0) > 50 ? '#ef4444' : (info.temperature || 0) > 40 ? '#eab308' : '#10b981';
            const rColor = (info.reallocated_sectors || 0) > 0 ? '#ef4444' : '#10b981';
            const poh = info.power_on_hours ? `${Math.floor(info.power_on_hours / 24)} dni (${info.power_on_hours.toLocaleString()} godz.)` : '-';
            smartBody.innerHTML = `
                <div class="sto-smart-grid">
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
            `;
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
        if (!confirm(`${t('Odmontować')} ${drive.mountpoint}?`)) return;
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
        if (!confirm(`${t('Bezpiecznie wysunąć')} /dev/${diskName}?\n${t('Wszystkie partycje zostaną odmontowane.')}`)) return;
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
            if (!confirm(`${t('Włączyć auto-remount dla')} ${drive.label || drive.name}?\n${t('Dysk będzie automatycznie montowany ponownie jeśli się odmontuje, a USB autosuspend zostanie wyłączony.')}`)) return;
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
        if (!confirm(`⚠️ ${t('UWAGA!')}\n\n${t('Czy na pewno sformatować')} /dev/${state.selected} ${t('na')} ${fsLabel}?\n\n${t('WSZYSTKIE DANE ZOSTANĄ UTRACONE!')}\n${t('Ta operacja jest NIEODWRACALNA!')}`)) return;

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
            if (!confirm(`⚠️ ${t('UWAGA!')}\n\n${t('Połączyć partycje na')} /dev/${parent.name}?\n\n${t('Partycje')} ${childParts.map(d => d.name).join(', ')} ${t('zostaną USUNIĘTE.')}\n${t('WSZYSTKIE DANE ZOSTANĄ UTRACONE!')}`)) return;

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
            if (!confirm(`⚠️ ${t('UWAGA!')}\n\n${t('Podzielić')} /dev/${parentDisk.name} ${t('na')} ${splitParts.length} ${t('partycji?')}\n\n${descs}\n\n${t('WSZYSTKIE DANE ZOSTANĄ UTRACONE!')}`)) return;

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
       Init
       ═══════════════════════════════════════════════════════ */
    $('#st-refresh').onclick = () => loadDrives();
    loadDrives();
}
