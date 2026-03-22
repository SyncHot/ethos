/* ═══════════════════════════════════════════════════════════
   EthOS — RAID / LVM Manager
   Create and manage mdadm arrays and LVM volumes.
   ═══════════════════════════════════════════════════════════ */

AppRegistry['raid'] = function (appDef) {
    createWindow('raid', {
        title: t('RAID / LVM'),
        icon: appDef.icon,
        iconColor: appDef.color,
        width: 1060,
        height: 700,
        onRender: (body) => renderRaidApp(body),
    });
};

function renderRaidApp(body) {
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
}
