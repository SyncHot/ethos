AppRegistry['vm-manager'] = function (appDef) {
    createWindow('vm-manager', {
        title: t('VM Manager'),
        icon: appDef.icon,
        iconColor: appDef.color,
        width: 1100,
        height: 750,
        onRender: (body) => renderVMManager(body),
    });
};

function renderVMManager(body) {
    const S = {
        tab: 'machines',
        machines: [],
        images: [],
        status: null,
        selectedVM: null,
        detailTab: 'info',
        snapshots: [],
        diskInfo: null,
        _intervals: [],
    };

    function addInterval(id) { S._intervals.push(id); }
    function clearAllIntervals() { S._intervals.forEach(clearInterval); S._intervals.length = 0; }
    function esc(s) { const d = document.createElement('div'); d.textContent = s || ''; return d.innerHTML; }

    body.innerHTML = `
        <div class="vm">
            <div class="vm-sidebar">
                <div class="vm-nav-item active" data-tab="machines"><i class="fas fa-desktop"></i> Maszyny</div>
                <div class="vm-nav-item" data-tab="images"><i class="fas fa-compact-disc"></i> Obrazy</div>
                <div class="vm-nav-item" data-tab="system"><i class="fas fa-microchip"></i> System</div>
            </div>
            <div class="vm-main" id="vm-main"></div>
        </div>
    `;

    body.querySelectorAll('.vm-nav-item').forEach(nav => {
        nav.addEventListener('click', () => {
            body.querySelectorAll('.vm-nav-item').forEach(n => n.classList.remove('active'));
            nav.classList.add('active');
            S.tab = nav.dataset.tab;
            S.selectedVM = null;
            clearAllIntervals();
            renderTab();
        });
    });

    const main = body.querySelector('#vm-main');

    function renderTab() {
        switch (S.tab) {
            case 'machines': renderMachinesTab(); break;
            case 'images': renderImagesTab(); break;
            case 'system': renderSystemTab(); break;
        }
    }

    // ─── MACHINES TAB ───

    async function loadMachines() {
        try { S.machines = await api('/vm/machines'); } catch { S.machines = []; }
    }

    function renderMachinesTab() {
        if (S.selectedVM) { renderVMDetail(); return; }
        main.innerHTML = `
            <div class="vm-toolbar">
                <span class="vm-toolbar-title"><i class="fas fa-desktop"></i> Wirtualne maszyny <span class="vm-badge" id="vm-cnt">0</span></span>
                <button class="vm-btn vm-btn-primary" id="vm-create-btn"><i class="fas fa-plus"></i> Nowa VM</button>
                <button class="vm-btn" id="vm-import-btn" title="${t('Importuj istniejący dysk qcow2/vmdk/vdi/raw')}"><i class="fas fa-file-import"></i> Importuj dysk</button>
                <button class="vm-btn" id="vm-refresh-btn"><i class="fas fa-sync-alt"></i></button>
            </div>
            <div class="vm-table-wrap">
                <table class="vm-table">
                    <thead><tr>
                        <th class="app-col-icon"></th>
                        <th>Nazwa</th>
                        <th>System</th>
                        <th>CPU</th>
                        <th>RAM</th>
                        <th>Dysk</th>
                        <th>Status</th>
                        <th class="app-col-actions-lg">Akcje</th>
                    </tr></thead>
                    <tbody id="vm-tbody"></tbody>
                </table>
            </div>
        `;
        main.querySelector('#vm-create-btn').addEventListener('click', showCreateModal);
        main.querySelector('#vm-import-btn').addEventListener('click', showImportDiskModal);
        main.querySelector('#vm-refresh-btn').addEventListener('click', async () => {
            await loadMachines(); fillMachinesTable();
        });
        loadMachines().then(fillMachinesTable);
    }

    function fillMachinesTable() {
        const tbody = main.querySelector('#vm-tbody');
        if (!tbody) return;
        const badge = main.querySelector('#vm-cnt');
        if (badge) badge.textContent = S.machines.length;

        if (!S.machines.length) {
            tbody.innerHTML = `<tr><td colspan="8" class="vm-empty-cell">${t('Brak wirtualnych maszyn — utwórz pierwszą!')}</td></tr>`;
            return;
        }

        const osIcons = { linux: 'fa-linux', windows: 'fa-windows', other: 'fa-question-circle' };
        tbody.innerHTML = S.machines.map(vm => {
            const running = vm.status === 'running';
            const statusDot = running ? `<span class="vm-dot vm-dot-running"></span> ${t('Działa')}` : '<span class="vm-dot vm-dot-stopped"></span> Zatrzymana';
            const osIcon = osIcons[vm.os_type] || 'fa-question-circle';
            const archBadge = vm.arch === 'raspi' ? '<span class="vm-arch-badge arm"><i class="fab fa-raspberry-pi"></i> RPi</span>'
                : vm.arch === 'aarch64' ? '<span class="vm-arch-badge arm">ARM64</span>'
                : '<span class="vm-arch-badge x86">x86_64</span>';
            const net = vm.network || { net_type: 'user', port_forwards: [] };
            let quickLinks = '';
            if (running && net.net_type === 'user' && net.port_forwards?.length) {
                quickLinks = net.port_forwards.map(pf => {
                    const lbl = pf.label ? esc(pf.label) : `${pf.guest}`;
                    return `<a href="http://${location.hostname}:${pf.host}" target="_blank" class="vm-link-chip" style="padding:3px 8px;font-size:11px" title="${pf.proto} :${pf.host}→:${pf.guest}" onclick="event.stopPropagation()"><i class="fas fa-external-link-alt"></i> ${lbl} :${pf.host}</a>`;
                }).join(' ');
            }
            return `<tr class="vm-row" data-id="${esc(vm.id)}">
                <td><i class="fab ${osIcon} app-os-icon"></i></td>
                <td><strong class="vm-name-link" data-id="${esc(vm.id)}">${esc(vm.name)}</strong>${quickLinks ? `<div class="vm-links-row" style="margin-top:4px">${quickLinks}</div>` : ''}</td>
                <td>${esc(vm.os_type)} ${archBadge}</td>
                <td>${vm.cpu} vCPU</td>
                <td>${vm.ram} MB</td>
                <td>${esc(vm.disk_size)}</td>
                <td>${statusDot}</td>
                <td class="vm-actions">
                    ${running
                        ? `<button class="vm-btn vm-btn-sm vm-btn-warn" data-action="stop" data-id="${esc(vm.id)}" title="Zatrzymaj"><i class="fas fa-stop"></i></button>
                           <button class="vm-btn vm-btn-sm" data-action="restart" data-id="${esc(vm.id)}" title="Restart"><i class="fas fa-redo"></i></button>`
                        : `<button class="vm-btn vm-btn-sm vm-btn-success" data-action="start" data-id="${esc(vm.id)}" title="Uruchom"><i class="fas fa-play"></i></button>`}
                    <button class="vm-btn vm-btn-sm vm-btn-danger" data-action="delete" data-id="${esc(vm.id)}" title="${t('Usuń')}"><i class="fas fa-trash"></i></button>
                </td>
            </tr>`;
        }).join('');

        // Row click → detail
        tbody.querySelectorAll('.vm-name-link').forEach(el => {
            el.addEventListener('click', () => {
                S.selectedVM = S.machines.find(v => v.id === el.dataset.id);
                if (S.selectedVM?.status === 'running' && S.selectedVM?.ws_port) S.detailTab = 'console';
                else S.detailTab = 'info';
                renderTab();
            });
        });

        // Action buttons
        tbody.querySelectorAll('.vm-btn[data-action]').forEach(btn => {
            btn.addEventListener('click', async (e) => {
                e.stopPropagation();
                const { action, id } = btn.dataset;
                if (action === 'start') await vmAction(id, 'start');
                else if (action === 'stop') await vmAction(id, 'stop');
                else if (action === 'restart') await vmAction(id, 'restart');
                else if (action === 'delete') {
                    if (!confirm(t('Usunąć tę maszynę wirtualną i jej dyski?'))) return;
                    try { await api(`/vm/machines/${id}`, { method: 'DELETE' }); toast(t('VM usunięta'), 'success'); }
                    catch { toast(t('Błąd usuwania'), 'error'); }
                }
                await loadMachines(); fillMachinesTable();
            });
        });
    }

    async function vmAction(id, action) {
        try {
            const r = await api(`/vm/machines/${id}/${action}`, { method: 'POST' });
            if (r && r.error) {
                toast(r.error, 'error');
            } else {
                toast(r.message || `${action} OK`, 'success');
            }
        } catch (e) {
            toast(e.message || `${t('Błąd:')} ${action}`, 'error');
        }
    }

    // ─── CREATE VM MODAL ───

    async function showCreateModal() {
        let imgs = [];
        try { imgs = await api('/vm/images'); } catch {}

        const overlay = document.createElement('div');
        overlay.className = 'vm-modal-overlay';
        overlay.innerHTML = `
            <div class="vm-modal">
                <div class="vm-modal-header">
                    <span>Nowa maszyna wirtualna</span>
                    <button class="vm-modal-close">&times;</button>
                </div>
                <div class="vm-modal-body">
                    <div class="vm-form-group">
                        <label>Nazwa</label>
                        <input type="text" id="vm-new-name" class="vm-input" placeholder="np. Ubuntu Server">
                    </div>
                    <div class="vm-form-row">
                        <div class="vm-form-group">
                            <label>CPU (rdzenie)</label>
                            <input type="number" id="vm-new-cpu" class="vm-input" value="2" min="1" max="32">
                        </div>
                        <div class="vm-form-group">
                            <label>RAM (MB)</label>
                            <input type="number" id="vm-new-ram" class="vm-input" value="2048" min="256" max="65536" step="256">
                        </div>
                    </div>
                    <div class="vm-form-row">
                        <div class="vm-form-group">
                            <label>Rozmiar dysku</label>
                            <input type="text" id="vm-new-disk" class="vm-input" value="20G" placeholder="np. 20G, 512M">
                        </div>
                        <div class="vm-form-group">
                            <label>Format dysku</label>
                            <select id="vm-new-diskfmt" class="vm-input">
                                <option value="qcow2" selected>QCOW2 (snapshoty, mniejszy)</option>
                                <option value="raw">RAW (szybszy I/O)</option>
                            </select>
                        </div>
                    </div>
                    <div class="vm-form-row">
                        <div class="vm-form-group">
                            <label>Typ systemu</label>
                            <select id="vm-new-os" class="vm-input">
                                <option value="linux">Linux</option>
                                <option value="windows">Windows</option>
                                <option value="other">Inny</option>
                            </select>
                        </div>
                        <div class="vm-form-group">
                            <label>Obraz rozruchowy (ISO/IMG)</label>
                            <select id="vm-new-image" class="vm-input">
                                <option value="">— brak —</option>
                                ${imgs.map(i => `<option value="${esc(i.path)}">${esc(i.name)} (${esc(i.size_human)})</option>`).join('')}
                            </select>
                        </div>
                    </div>
                    <div class="vm-form-group">
                        <label>Opis (opcjonalnie)</label>
                        <input type="text" id="vm-new-desc" class="vm-input" placeholder="${t('Krótki opis...')}">
                    </div>
                </div>
                <div class="vm-modal-footer">
                    <button class="vm-btn" id="vm-modal-cancel">Anuluj</button>
                    <button class="vm-btn vm-btn-primary" id="vm-modal-ok">${t('Utwórz')}</button>
                </div>
            </div>
        `;
        body.appendChild(overlay);

        const close = () => overlay.remove();
        overlay.querySelector('.vm-modal-close').addEventListener('click', close);
        overlay.querySelector('#vm-modal-cancel').addEventListener('click', close);
        overlay.addEventListener('click', e => { if (e.target === overlay) close(); });

        overlay.querySelector('#vm-modal-ok').addEventListener('click', async () => {
            const name = overlay.querySelector('#vm-new-name').value.trim();
            if (!name) { toast(t('Podaj nazwę VM'), 'warning'); return; }
            const payload = {
                name,
                cpu: parseInt(overlay.querySelector('#vm-new-cpu').value) || 2,
                ram: parseInt(overlay.querySelector('#vm-new-ram').value) || 2048,
                disk_size: overlay.querySelector('#vm-new-disk').value || '20G',
                disk_format: overlay.querySelector('#vm-new-diskfmt').value || 'qcow2',
                os_type: overlay.querySelector('#vm-new-os').value || 'linux',
                boot_image: overlay.querySelector('#vm-new-image').value || '',
                description: overlay.querySelector('#vm-new-desc').value || '',
            };
            try {
                const r = await api('/vm/machines', { method: 'POST', body: payload });
                toast(r.message || 'VM utworzona', 'success');
                close();
                await loadMachines(); fillMachinesTable();
            } catch (e) {
                toast(e.message || t('Błąd tworzenia VM'), 'error');
            }
        });
    }


    // ─── IMPORT DISK MODAL ───

    async function showImportDiskModal() {
        const overlay = document.createElement('div');
        overlay.className = 'vm-modal-overlay';
        overlay.innerHTML = `
            <div class="vm-modal" style="max-width:540px">
                <div class="vm-modal-header">
                    <span><i class="fas fa-file-import" style="color:#7c3aed;margin-right:8px"></i>Importuj dysk VM</span>
                    <button class="vm-modal-close">&times;</button>
                </div>
                <div class="vm-modal-body">
                    <div class="vm-form-group">
                        <label>${t('Nazwa VM')}</label>
                        <input type="text" id="vi-name" class="vm-input" placeholder="np. Ubuntu Import">
                    </div>
                    <div class="vm-form-row">
                        <div class="vm-form-group">
                            <label>CPU (rdzenie)</label>
                            <input type="number" id="vi-cpu" class="vm-input" value="2" min="1" max="32">
                        </div>
                        <div class="vm-form-group">
                            <label>RAM (MB)</label>
                            <input type="number" id="vi-ram" class="vm-input" value="2048" min="256" max="65536" step="256">
                        </div>
                    </div>
                    <div class="vm-form-row">
                        <div class="vm-form-group">
                            <label>${t('Typ systemu')}</label>
                            <select id="vi-os" class="vm-input">
                                <option value="linux">Linux</option>
                                <option value="windows">Windows</option>
                                <option value="other">Inny</option>
                            </select>
                        </div>
                        <div class="vm-form-group">
                            <label>${t('Konwertuj do QCOW2')}</label>
                            <select id="vi-convert" class="vm-input">
                                <option value="true">${t('Tak (zalecane, snapshoty)')}</option>
                                <option value="false">${t('Nie (zachowaj format)')}</option>
                            </select>
                        </div>
                    </div>
                    <div class="vm-form-group">
                        <label>${t('Plik dysku')} <span style="color:var(--text-muted);font-weight:400">(.qcow2, .vmdk, .vdi, .raw, .img, .vhd)</span></label>
                        <div style="display:flex;gap:8px;align-items:center">
                            <input type="file" id="vi-file" accept=".qcow2,.vmdk,.vdi,.raw,.img,.vhd,.vhdx" style="flex:1;font-size:13px;color:var(--text-primary)">
                        </div>
                    </div>
                    <div id="vi-progress-wrap" style="display:none">
                        <div style="display:flex;justify-content:space-between;font-size:12px;color:var(--text-muted);margin-bottom:4px">
                            <span id="vi-progress-label">${t('Przesyłanie...')}</span>
                            <span id="vi-progress-pct">0%</span>
                        </div>
                        <div style="background:var(--bg-surface-alt);border-radius:999px;height:6px;overflow:hidden">
                            <div id="vi-progress-bar" style="height:100%;background:#7c3aed;width:0%;transition:width .3s;border-radius:999px"></div>
                        </div>
                    </div>
                    <div id="vi-error" style="display:none;color:#ef4444;font-size:13px;margin-top:8px;padding:8px 10px;background:rgba(239,68,68,.08);border-radius:6px"></div>
                </div>
                <div class="vm-modal-footer">
                    <button class="vm-btn" id="vi-cancel">${t('Anuluj')}</button>
                    <button class="vm-btn vm-btn-primary" id="vi-ok" style="background:#7c3aed;border-color:#7c3aed"><i class="fas fa-file-import"></i> ${t('Importuj')}</button>
                </div>
            </div>
        `;
        body.appendChild(overlay);

        const close = () => overlay.remove();
        overlay.querySelector('.vm-modal-close').addEventListener('click', close);
        overlay.querySelector('#vi-cancel').addEventListener('click', close);
        overlay.addEventListener('click', e => { if (e.target === overlay) close(); });

        overlay.querySelector('#vi-ok').addEventListener('click', () => {
            const name = overlay.querySelector('#vi-name').value.trim();
            if (!name) { toast(t('Podaj nazwę VM'), 'warning'); return; }
            const file = overlay.querySelector('#vi-file').files[0];
            if (!file) { toast(t('Wybierz plik dysku'), 'warning'); return; }

            const errEl = overlay.querySelector('#vi-error');
            const progWrap = overlay.querySelector('#vi-progress-wrap');
            const progBar = overlay.querySelector('#vi-progress-bar');
            const progPct = overlay.querySelector('#vi-progress-pct');
            const progLabel = overlay.querySelector('#vi-progress-label');
            const okBtn = overlay.querySelector('#vi-ok');

            errEl.style.display = 'none';
            progWrap.style.display = 'block';
            okBtn.disabled = true;
            overlay.querySelector('#vi-cancel').disabled = true;

            const fd = new FormData();
            fd.append('file', file);
            fd.append('name', name);
            fd.append('cpu', overlay.querySelector('#vi-cpu').value);
            fd.append('ram', overlay.querySelector('#vi-ram').value);
            fd.append('os_type', overlay.querySelector('#vi-os').value);
            fd.append('convert', overlay.querySelector('#vi-convert').value);

            const token = NAS && NAS.token;
            const xhr = new XMLHttpRequest();
            xhr.open('POST', '/api/vm/import-disk');
            if (token) xhr.setRequestHeader('Authorization', 'Bearer ' + token);

            xhr.upload.addEventListener('progress', e => {
                if (e.lengthComputable) {
                    const pct = Math.round(e.loaded / e.total * 100);
                    progBar.style.width = (pct * 0.7) + '%'; // upload = 0-70%
                    progPct.textContent = pct + '%';
                    progLabel.textContent = pct < 100 ? t('Przesyłanie...') : t('Konwertowanie...');
                }
            });

            xhr.addEventListener('load', async () => {
                progBar.style.width = '100%';
                progPct.textContent = '100%';
                progLabel.textContent = t('Gotowe');

                if (xhr.status === 413) {
                    errEl.textContent = t('Plik zbyt duży – sprawdź limit nginx (client_max_body_size)');
                    errEl.style.display = 'block';
                    progWrap.style.display = 'none';
                    okBtn.disabled = false;
                    overlay.querySelector('#vi-cancel').disabled = false;
                    return;
                }

                try {
                    const resp = JSON.parse(xhr.responseText);
                    if (xhr.status >= 400 || resp.error) {
                        errEl.textContent = resp.error || t('Błąd importu');
                        errEl.style.display = 'block';
                        progWrap.style.display = 'none';
                        okBtn.disabled = false;
                        overlay.querySelector('#vi-cancel').disabled = false;
                        return;
                    }
                    toast(t('Dysk zaimportowany:') + ' ' + resp.name, 'success');
                    close();
                    await loadMachines(); fillMachinesTable();
                } catch (e) {
                    const msg = xhr.status ? `HTTP ${xhr.status}` : t('Nieoczekiwany błąd');
                    errEl.textContent = t('Błąd serwera:') + ' ' + msg;
                    errEl.style.display = 'block';
                    progWrap.style.display = 'none';
                    okBtn.disabled = false;
                    overlay.querySelector('#vi-cancel').disabled = false;
                }
            });

            xhr.addEventListener('error', () => {
                errEl.textContent = t('Błąd sieci podczas przesyłania');
                errEl.style.display = 'block';
                progWrap.style.display = 'none';
                okBtn.disabled = false;
                overlay.querySelector('#vi-cancel').disabled = false;
            });

            xhr.send(fd);
        });
    }

    // ─── VM DETAIL VIEW ───

    function renderVMDetail() {
        const vm = S.selectedVM;
        if (!vm) { renderMachinesTab(); return; }
        const running = vm.status === 'running';

        main.innerHTML = `
            <div class="vm-toolbar">
                <button class="vm-btn" id="vm-back"><i class="fas fa-arrow-left"></i> ${t('Powrót')}</button>
                <span class="vm-toolbar-title app-ml-md">${esc(vm.name)}</span>
                <span class="app-toolbar-actions">
                    ${running
                        ? `<button class="vm-btn vm-btn-warn" id="vm-d-stop"><i class="fas fa-stop"></i> Zatrzymaj</button>
                           <button class="vm-btn" id="vm-d-restart"><i class="fas fa-redo"></i> Restart</button>`
                        : `<button class="vm-btn vm-btn-success" id="vm-d-start"><i class="fas fa-play"></i> Uruchom</button>`}
                </span>
            </div>
            ${running && vm.ws_port ? `
            <div class="vm-vnc-bar">
                <i class="fas fa-tv"></i>
                <span>${t('Konsola dostępna w zakładce')} <strong>${t('Konsola')}</strong> ${t('poniżej')}</span>
                <span class="vm-vnc-hint">VNC: ${location.hostname}:${vm.vnc_port} | WS: ${vm.ws_port}</span>
            </div>` : running && vm.vnc_port ? `
            <div class="vm-vnc-bar">
                <i class="fas fa-tv"></i>
                <span>${t('VNC:')} <strong>${location.hostname}:${vm.vnc_port}</strong></span>
                <span class="vm-vnc-hint">${t('Połącz klientem VNC (np. TigerVNC, Remmina)')}</span>
            </div>` : ''}
            <div class="vm-detail-tabs">
                ${running && vm.ws_port ? `<div class="vm-dtab ${S.detailTab === 'console' ? 'active' : ''}" data-t="console"><i class="fas fa-tv"></i> Konsola</div>` : ''}
                <div class="vm-dtab ${S.detailTab === 'info' ? 'active' : ''}" data-t="info">Konfiguracja</div>
                <div class="vm-dtab ${S.detailTab === 'network' ? 'active' : ''}" data-t="network"><i class="fas fa-network-wired"></i> Sieć</div>
                <div class="vm-dtab ${S.detailTab === 'snapshots' ? 'active' : ''}" data-t="snapshots">Snapshoty</div>
                <div class="vm-dtab ${S.detailTab === 'disk' ? 'active' : ''}" data-t="disk">Dysk</div>
            </div>
            <div id="vm-detail-content"></div>
        `;

        main.querySelector('#vm-back').addEventListener('click', () => { S.selectedVM = null; renderTab(); });

        // Power buttons
        main.querySelector('#vm-d-start')?.addEventListener('click', async () => {
            await vmAction(vm.id, 'start'); await refreshSelectedVM();
        });
        main.querySelector('#vm-d-stop')?.addEventListener('click', async () => {
            await vmAction(vm.id, 'stop'); await refreshSelectedVM();
        });
        main.querySelector('#vm-d-restart')?.addEventListener('click', async () => {
            await vmAction(vm.id, 'restart'); await refreshSelectedVM();
        });

        // Detail sub-tabs
        main.querySelectorAll('.vm-dtab').forEach(tab => {
            tab.addEventListener('click', () => {
                S.detailTab = tab.dataset.t;
                main.querySelectorAll('.vm-dtab').forEach(t => t.classList.toggle('active', t.dataset.t === S.detailTab));
                renderDetailContent();
            });
        });

        renderDetailContent();
    }

    async function refreshSelectedVM() {
        await loadMachines();
        if (S.selectedVM) {
            S.selectedVM = S.machines.find(v => v.id === S.selectedVM.id);
            if (!S.selectedVM) { renderTab(); return; }
        }
        renderVMDetail();
    }

    function renderDetailContent() {
        const dc = main.querySelector('#vm-detail-content');
        if (!dc) return;
        switch (S.detailTab) {
            case 'console': renderConsolePanel(dc); break;
            case 'info': renderInfoPanel(dc); break;
            case 'network': renderNetworkPanel(dc); break;
            case 'snapshots': renderSnapshotsPanel(dc); break;
            case 'disk': renderDiskPanel(dc); break;
        }
    }

    // Console panel (noVNC)
    function renderConsolePanel(dc) {
        const vm = S.selectedVM;
        if (!vm || vm.status !== 'running' || !vm.ws_port) {
            dc.innerHTML = `<div class="vm-empty">${t('Konsola dostępna tylko dla działających maszyn z aktywnym WebSocket.')}</div>`;
            return;
        }
        const wsHost = location.hostname;
        // Serve noVNC through Flask (same-origin) so iframe isn't blocked by CSP.
        // WebSocket connects directly to websockify port.
        const novncUrl = `/api/vm/novnc/vnc_lite.html?host=${wsHost}&port=${vm.ws_port}&autoconnect=true&resize=scale&reconnect=true&path=websockify`;
        const directUrl = `http://${wsHost}:${vm.ws_port}/vnc_lite.html?host=${wsHost}&port=${vm.ws_port}&autoconnect=true&resize=scale&reconnect=true`;
        dc.innerHTML = `
            <div class="vm-console-wrap">
                <div class="vm-console-toolbar">
                    <span><i class="fas fa-tv"></i> Konsola — ${esc(vm.name)}</span>
                    <a href="${directUrl}" target="_blank" class="vm-btn vm-btn-sm" title="Otwórz w nowej karcie"><i class="fas fa-external-link-alt"></i></a>
                    <button class="vm-btn vm-btn-sm" id="vm-console-fullscreen" title="${t('Pełny ekran')}"><i class="fas fa-expand"></i></button>
                </div>
                <iframe id="vm-console-frame" class="vm-console-iframe" src="${novncUrl}" allowfullscreen></iframe>
            </div>
        `;
        const frame = dc.querySelector('#vm-console-frame');
        dc.querySelector('#vm-console-fullscreen')?.addEventListener('click', () => {
            if (frame.requestFullscreen) frame.requestFullscreen();
            else if (frame.webkitRequestFullscreen) frame.webkitRequestFullscreen();
        });
    }

    // Info / Config panel
    function renderInfoPanel(dc) {
        const vm = S.selectedVM;
        const running = vm.status === 'running';
        const osLabels = { linux: 'Linux', windows: 'Windows', other: 'Inny' };
        const host = location.hostname;
        const net = vm.network || { net_type: 'user', port_forwards: [] };

        // Build connection links for running VMs
        let linksHtml = '';
        if (running) {
            const links = [];
            if (net.net_type === 'user' && net.port_forwards?.length) {
                for (const pf of net.port_forwards) {
                    const url = `http://${host}:${pf.host}`;
                    const label = pf.label ? esc(pf.label) : `${pf.proto}/${pf.guest}`;
                    links.push(`<a href="${esc(url)}" target="_blank" class="vm-link-chip" title="${esc(pf.proto)} host:${pf.host} → guest:${pf.guest}"><i class="fas fa-external-link-alt"></i> ${label} <span class="vm-link-port">:${pf.host}</span></a>`);
                }
            }
            if (vm.ws_port) {
                const vncUrl = `http://${host}:${vm.ws_port}/vnc_lite.html?host=${host}&port=${vm.ws_port}&autoconnect=true&resize=scale&reconnect=true`;
                links.push(`<a href="${esc(vncUrl)}" target="_blank" class="vm-link-chip vm-link-vnc" title="Otwórz konsolę VNC w przeglądarce"><i class="fas fa-tv"></i> Konsola VNC <span class="vm-link-port">:${vm.ws_port}</span></a>`);
            } else if (vm.vnc_port) {
                links.push(`<span class="vm-link-chip vm-link-vnc" title="Połącz klientem VNC na ${host}:${vm.vnc_port}"><i class="fas fa-tv"></i> VNC <span class="vm-link-port">:${vm.vnc_port}</span></span>`);
            }
            if (links.length) {
                linksHtml = `
                <div class="vm-info-card" style="grid-column:1/-1">
                    <h4><i class="fas fa-link"></i> Połączenia</h4>
                    <div class="vm-links-row">${links.join(' ')}</div>
                </div>`;
            }
        }

        dc.innerHTML = `
            <div class="vm-info-grid">
                ${linksHtml}
                <div class="vm-info-card">
                    <h4><i class="fas fa-info-circle"></i> Informacje</h4>
                    <div class="vm-info-row"><span>Nazwa:</span><span>${esc(vm.name)}</span></div>
                    <div class="vm-info-row"><span>ID:</span><span class="vm-mono">${esc(vm.id)}</span></div>
                    <div class="vm-info-row"><span>System:</span><span>${osLabels[vm.os_type] || vm.os_type}</span></div>
                    <div class="vm-info-row"><span>Architektura:</span><span>${vm.arch === 'raspi' ? '<span class="vm-arch-badge arm"><i class="fab fa-raspberry-pi"></i> Raspberry Pi</span> (raspi3b)'
                        : vm.arch === 'aarch64' ? '<span class="vm-arch-badge arm">ARM64</span> (emulacja)'
                        : '<span class="vm-arch-badge x86">x86_64</span>' + (vm.status === 'running' ? ' (KVM)' : '')}</span></div>
                    <div class="vm-info-row"><span>Opis:</span><span>${esc(vm.description) || '—'}</span></div>
                    <div class="vm-info-row"><span>Utworzona:</span><span>${esc(vm.created)}</span></div>
                    <div class="vm-info-row"><span>Status:</span><span>${running ? `<span class="vm-dot vm-dot-running"></span> ${t('Działa')}` : '<span class="vm-dot vm-dot-stopped"></span> Zatrzymana'}</span></div>
                    ${running && vm.pid ? `<div class="vm-info-row"><span>PID:</span><span>${vm.pid}</span></div>` : ''}
                </div>
                <div class="vm-info-card">
                    <h4><i class="fas fa-sliders-h"></i> Zasoby ${!running ? '<button class="vm-btn vm-btn-sm app-ml-auto" id="vm-edit-config"><i class="fas fa-edit"></i> Edytuj</button>' : ''}</h4>
                    <div class="vm-info-row"><span>CPU:</span><span id="vm-cfg-cpu">${vm.cpu} rdzeni</span></div>
                    <div class="vm-info-row"><span>RAM:</span><span id="vm-cfg-ram">${vm.ram} MB</span></div>
                    <div class="vm-info-row"><span>Dysk:</span><span>${esc(vm.disk_size)}</span></div>
                    <div class="vm-info-row"><span>Obraz boot:</span><span class="vm-boot-image-cell">${vm.boot_image
                        ? `<i class="fas fa-usb" style="color:#f59e0b;margin-right:4px"></i>${esc(vm.boot_image.split('/').pop())}${!running ? ' <button class="vm-btn vm-btn-xs vm-btn-danger" id="vm-eject-boot" title="Odłącz obraz (jak wyjęcie pendrive)"><i class="fas fa-eject"></i> Odłącz</button>' : ''}`
                        : `<span style="opacity:.5">— brak —</span>${!running ? ' <button class="vm-btn vm-btn-xs" id="vm-attach-boot" title="Podłącz obraz rozruchowy"><i class="fas fa-plug"></i> Podłącz</button>' : ''}`
                    }</span></div>
                </div>
            </div>
        `;

        main.querySelector('#vm-edit-config')?.addEventListener('click', () => showEditModal(vm));

        main.querySelector('#vm-eject-boot')?.addEventListener('click', async () => {
            if (!confirm(t('Odłączyć obraz boot? VM będzie bootować z dysku.'))) return;
            try {
                await api(`/vm/machines/${vm.id}`, { method: 'PUT', body: { boot_image: '' } });
                toast(t('Obraz odłączony — VM będzie bootować z dysku'), 'success');
                await refreshSelectedVM();
            } catch (e) { toast(e.message || t('Błąd'), 'error'); }
        });

        main.querySelector('#vm-attach-boot')?.addEventListener('click', () => showEditModal(vm));
    }

    // Network panel
    function renderNetworkPanel(dc) {
        const vm = S.selectedVM;
        const running = vm.status === 'running';
        const net = vm.network || { net_type: 'user', port_forwards: [] };
        const pf = net.port_forwards || [];
        const isUser = net.net_type === 'user';
        const isBridge = net.net_type === 'bridge';
        const isNone = net.net_type === 'none';

        let netInfoHtml = '';
        if (isUser) {
            netInfoHtml = `
                <div class="vm-info-row"><span>Typ:</span><span>User-mode NAT (QEMU SLIRP)</span></div>
                <div class="vm-info-row"><span>IP gościa:</span><span class="vm-mono">10.0.2.15</span></div>
                <div class="vm-info-row"><span>Gateway:</span><span class="vm-mono">10.0.2.2</span></div>
                <div class="vm-info-row"><span>DNS:</span><span class="vm-mono">10.0.2.3</span></div>`;
        } else if (isBridge) {
            netInfoHtml = `
                <div class="vm-info-row"><span>Typ:</span><span>Bridge (TAP) — VM dostaje własne IP z sieci LAN</span></div>
                <div class="vm-info-row"><span>Bridge:</span><span class="vm-mono">${esc(net.bridge || 'br0')}</span></div>
                <div class="vm-info-row"><span>IP gościa:</span><span>DHCP z routera (widoczne po starcie)</span></div>
                <div id="vm-bridge-status"></div>`;
        } else {
            netInfoHtml = `<div class="vm-empty" style="margin:8px 0">${t('Sieć wyłączona — VM nie ma dostępu do sieci.')}</div>`;
        }

        dc.innerHTML = `
            <div class="vm-info-grid">
                <div class="vm-info-card" style="grid-column:1/-1">
                    <h4><i class="fas fa-network-wired"></i> Tryb sieci
                        ${!running ? `<select id="vm-net-type" class="vm-input" style="width:auto;display:inline-block;margin-left:12px;font-size:12px">
                            <option value="user" ${isUser ? 'selected' : ''}>NAT (User-mode)</option>
                            <option value="bridge" ${isBridge ? 'selected' : ''}>Bridge (własne IP w LAN)</option>
                            <option value="none" ${isNone ? 'selected' : ''}>Wyłączona</option>
                        </select>` : `<span class="vm-arch-badge ${isBridge ? 'arm' : isUser ? 'x86' : ''}" style="margin-left:8px">${isBridge ? 'Bridge' : isUser ? 'NAT' : 'Wyłączona'}</span>`}
                    </h4>
                    ${netInfoHtml}
                </div>
            </div>
            ${isUser ? `
            <div class="vm-toolbar app-toolbar-flat" style="margin-top:16px">
                <span class="vm-toolbar-title">
                    <i class="fas fa-exchange-alt"></i> Port forwarding
                    <span class="vm-badge">${pf.length}</span>
                </span>
                ${!running ? `<button class="vm-btn vm-btn-primary vm-btn-sm" id="vm-pf-add">
                    <i class="fas fa-plus"></i> Dodaj regułę
                </button>` : ''}
            </div>
            ${pf.length ? `
            <table class="vm-table">
                <thead><tr>
                    <th>Etykieta</th>
                    <th>Protokół</th>
                    <th>Port hosta</th>
                    <th>Port gościa</th>
                    ${!running ? '<th class="app-col-actions-sm">Akcje</th>' : ''}
                </tr></thead>
                <tbody>${pf.map((r, i) => `<tr>
                    <td>${esc(r.label) || '—'}</td>
                    <td><span class="vm-arch-badge x86">${esc(r.proto).toUpperCase()}</span></td>
                    <td class="vm-mono">${r.host === 0 ? '<em>auto</em>' : r.host}</td>
                    <td class="vm-mono">${r.guest}</td>
                    ${!running ? `<td><button class="vm-btn vm-btn-sm vm-btn-danger" data-pf-del="${i}" title="${t('Usuń')}"><i class="fas fa-trash"></i></button></td>` : ''}
                </tr>`).join('')}</tbody>
            </table>` : `<div class="vm-empty">${t('Brak reguł port forwarding. Dodaj regułę, aby przekierować port z hosta do VM.')}</div>`}
            ` : ''}
            ${isBridge ? `
            <div class="vm-empty" style="margin-top:16px">
                <i class="fas fa-info-circle"></i> W trybie bridge port forwarding nie jest potrzebny — VM jest dostępna bezpośrednio pod własnym IP w sieci LAN.
            </div>` : ''}
        `;

        // Load bridge status if bridge mode
        if (isBridge) {
            (async () => {
                try {
                    const bs = await api('/vm/bridge');
                    const el = dc.querySelector('#vm-bridge-status');
                    if (el) {
                        if (bs.ready) {
                            el.innerHTML = `
                                <div class="vm-info-row"><span>Bridge IP:</span><span class="vm-mono">${esc(bs.bridge_ip)}</span></div>
                                <div class="vm-info-row"><span>Status:</span><span class="app-text-ok"><i class="fas fa-check-circle"></i> Gotowy</span></div>
                                ${!running ? `<button class="vm-btn vm-btn-secondary vm-btn-sm" id="vm-bridge-reset" style="margin-top:8px">
                                    <i class="fas fa-redo"></i> Resetuj bridge
                                </button>` : ''}`;
                            dc.querySelector('#vm-bridge-reset')?.addEventListener('click', async () => {
                                if (!confirm(t('Resetować bridge? Połączenie zostanie chwilowo przerwane.'))) return;
                                try {
                                    const r1 = await api('/vm/bridge/teardown', { method: 'POST' });
                                    toast(r1.message || 'Bridge usunięty', 'info');
                                    await new Promise(res => setTimeout(res, 2000));
                                    const r2 = await api('/vm/bridge/setup', { method: 'POST' });
                                    toast(r2.message || 'Bridge skonfigurowany', 'success');
                                    renderNetworkPanel(dc);
                                } catch (err) { toast(err.message || t('Błąd resetu bridge'), 'error'); }
                            });
                        } else {
                            el.innerHTML = `
                                <div class="vm-info-row"><span>Status:</span><span class="app-text-warn"><i class="fas fa-exclamation-triangle"></i> Bridge nie skonfigurowany</span></div>
                                ${!running ? `<button class="vm-btn vm-btn-primary vm-btn-sm" id="vm-bridge-setup" style="margin-top:8px">
                                    <i class="fas fa-cog"></i> Skonfiguruj bridge
                                </button>` : ''}`;
                            dc.querySelector('#vm-bridge-setup')?.addEventListener('click', async () => {
                                try {
                                    const r = await api('/vm/bridge/setup', { method: 'POST' });
                                    toast(r.message || 'Bridge skonfigurowany', 'success');
                                    renderNetworkPanel(dc);
                                } catch (err) { toast(err.message || t('Błąd konfiguracji bridge'), 'error'); }
                            });
                        }
                    }
                } catch {}
            })();
        }

        // Net type change
        dc.querySelector('#vm-net-type')?.addEventListener('change', async (e) => {
            const newType = e.target.value;
            const newNet = { ...net, net_type: newType };
            if (newType === 'bridge') newNet.bridge = 'br0';
            try {
                await api(`/vm/machines/${vm.id}/network`, { method: 'PUT', body: newNet });
                toast('Tryb sieci zmieniony', 'success');
                await refreshSelectedVM();
            } catch (err) { toast(err.message || t('Błąd'), 'error'); }
        });

        // Delete port forward rule
        dc.querySelectorAll('[data-pf-del]').forEach(btn => {
            btn.addEventListener('click', async () => {
                const idx = parseInt(btn.dataset.pfDel);
                const newPf = pf.filter((_, i) => i !== idx);
                try {
                    await api(`/vm/machines/${vm.id}/network`, { method: 'PUT', body: { ...net, port_forwards: newPf } });
                    toast('Reguła usunięta', 'success');
                    await refreshSelectedVM();
                } catch (err) { toast(err.message || t('Błąd'), 'error'); }
            });
        });

        // Add port forward rule
        dc.querySelector('#vm-pf-add')?.addEventListener('click', () => {
            showAddPortForwardModal(vm, net);
        });
    }

    function showAddPortForwardModal(vm, net) {
        const overlay = document.createElement('div');
        overlay.className = 'vm-modal-overlay';
        overlay.innerHTML = `
            <div class="vm-modal" style="max-width:420px">
                <div class="vm-modal-header">
                    <span><i class="fas fa-exchange-alt"></i> Nowa reguła port forwarding</span>
                    <button class="vm-modal-close">&times;</button>
                </div>
                <div class="vm-modal-body">
                    <div class="vm-form-group">
                        <label>Etykieta (opcjonalnie)</label>
                        <input type="text" id="vm-pf-label" class="vm-input" placeholder="np. SSH, HTTP, Webserver">
                    </div>
                    <div class="vm-form-row">
                        <div class="vm-form-group">
                            <label>Protokół</label>
                            <select id="vm-pf-proto" class="vm-input">
                                <option value="tcp">TCP</option>
                                <option value="udp">UDP</option>
                            </select>
                        </div>
                        <div class="vm-form-group">
                            <label>Port gościa (VM)</label>
                            <input type="number" id="vm-pf-guest" class="vm-input" min="1" max="65535" placeholder="np. 22, 80, 443">
                        </div>
                    </div>
                    <div class="vm-form-group">
                        <label>Port hosta (0 = automatyczny)</label>
                        <input type="number" id="vm-pf-host" class="vm-input" min="0" max="65535" value="0">
                        <small style="color:var(--text-muted);font-size:11px">0 = system wybierze wolny port automatycznie</small>
                    </div>
                </div>
                <div class="vm-modal-footer">
                    <button class="vm-btn" id="vm-pf-cancel">Anuluj</button>
                    <button class="vm-btn vm-btn-primary" id="vm-pf-ok">Dodaj</button>
                </div>
            </div>
        `;
        body.appendChild(overlay);

        const close = () => overlay.remove();
        overlay.querySelector('.vm-modal-close').addEventListener('click', close);
        overlay.querySelector('#vm-pf-cancel').addEventListener('click', close);
        overlay.addEventListener('click', e => { if (e.target === overlay) close(); });

        overlay.querySelector('#vm-pf-ok').addEventListener('click', async () => {
            const guest = parseInt(overlay.querySelector('#vm-pf-guest').value);
            if (!guest || guest < 1 || guest > 65535) {
                toast('Podaj prawidłowy port gościa (1-65535)', 'warning');
                return;
            }
            const rule = {
                proto: overlay.querySelector('#vm-pf-proto').value,
                host: parseInt(overlay.querySelector('#vm-pf-host').value) || 0,
                guest,
                label: overlay.querySelector('#vm-pf-label').value.trim(),
            };
            const newPf = [...(net.port_forwards || []), rule];
            try {
                await api(`/vm/machines/${vm.id}/network`, { method: 'PUT', body: { ...net, port_forwards: newPf } });
                toast('Reguła dodana', 'success');
                close();
                await refreshSelectedVM();
            } catch (err) { toast(err.message || t('Błąd'), 'error'); }
        });
    }

    // Edit VM modal
    async function showEditModal(vm) {
        let imgs = [];
        try { imgs = await api('/vm/images'); } catch {}

        const overlay = document.createElement('div');
        overlay.className = 'vm-modal-overlay';
        overlay.innerHTML = `
            <div class="vm-modal">
                <div class="vm-modal-header">
                    <span>Edytuj: ${esc(vm.name)}</span>
                    <button class="vm-modal-close">&times;</button>
                </div>
                <div class="vm-modal-body">
                    <div class="vm-form-group">
                        <label>Nazwa</label>
                        <input type="text" id="vm-e-name" class="vm-input" value="${esc(vm.name)}">
                    </div>
                    <div class="vm-form-row">
                        <div class="vm-form-group">
                            <label>CPU (rdzenie)</label>
                            <input type="number" id="vm-e-cpu" class="vm-input" value="${vm.cpu}" min="1" max="32">
                        </div>
                        <div class="vm-form-group">
                            <label>RAM (MB)</label>
                            <input type="number" id="vm-e-ram" class="vm-input" value="${vm.ram}" min="256" max="65536" step="256">
                        </div>
                    </div>
                    <div class="vm-form-row">
                        <div class="vm-form-group">
                            <label>Typ systemu</label>
                            <select id="vm-e-os" class="vm-input">
                                <option value="linux" ${vm.os_type === 'linux' ? 'selected' : ''}>Linux</option>
                                <option value="windows" ${vm.os_type === 'windows' ? 'selected' : ''}>Windows</option>
                                <option value="other" ${vm.os_type === 'other' ? 'selected' : ''}>Inny</option>
                            </select>
                        </div>
                        <div class="vm-form-group">
                            <label>Obraz rozruchowy</label>
                            <select id="vm-e-image" class="vm-input">
                                <option value="">— brak —</option>
                                ${imgs.map(i => `<option value="${esc(i.path)}" ${vm.boot_image === i.path ? 'selected' : ''}>${esc(i.name)}</option>`).join('')}
                            </select>
                        </div>
                    </div>
                    <div class="vm-form-group">
                        <label>Opis</label>
                        <input type="text" id="vm-e-desc" class="vm-input" value="${esc(vm.description || '')}">
                    </div>
                </div>
                <div class="vm-modal-footer">
                    <button class="vm-btn" id="vm-e-cancel">Anuluj</button>
                    <button class="vm-btn vm-btn-primary" id="vm-e-save">Zapisz</button>
                </div>
            </div>
        `;
        body.appendChild(overlay);

        const close = () => overlay.remove();
        overlay.querySelector('.vm-modal-close').addEventListener('click', close);
        overlay.querySelector('#vm-e-cancel').addEventListener('click', close);
        overlay.addEventListener('click', e => { if (e.target === overlay) close(); });

        overlay.querySelector('#vm-e-save').addEventListener('click', async () => {
            const payload = {
                name: overlay.querySelector('#vm-e-name').value.trim(),
                cpu: parseInt(overlay.querySelector('#vm-e-cpu').value) || vm.cpu,
                ram: parseInt(overlay.querySelector('#vm-e-ram').value) || vm.ram,
                os_type: overlay.querySelector('#vm-e-os').value,
                boot_image: overlay.querySelector('#vm-e-image').value,
                description: overlay.querySelector('#vm-e-desc').value,
            };
            try {
                await api(`/vm/machines/${vm.id}`, { method: 'PUT', body: payload });
                toast('Konfiguracja zapisana', 'success');
                close();
                await refreshSelectedVM();
            } catch (e) {
                toast(e.message || t('Błąd zapisu'), 'error');
            }
        });
    }

    // Snapshots panel
    async function renderSnapshotsPanel(dc) {
        const vm = S.selectedVM;
        dc.innerHTML = `<div class="vm-loading"><i class="fas fa-spinner fa-spin"></i> ${t('Ładowanie snapshotów...')}</div>`;
        try {
            const res = await api(`/vm/machines/${vm.id}/snapshots`);
            S.snapshots = res.snapshots || [];
        } catch (e) {
            dc.innerHTML = `<div class="vm-empty">${esc(e.message || t('Błąd pobierania snapshotów'))}</div>`;
            return;
        }

        dc.innerHTML = `
            <div class="vm-toolbar app-toolbar-flat">
                <span class="vm-toolbar-title"><i class="fas fa-camera"></i> Snapshoty <span class="vm-badge">${S.snapshots.length}</span></span>
                <button class="vm-btn vm-btn-primary vm-btn-sm" id="vm-snap-create"><i class="fas fa-plus"></i> Nowy</button>
            </div>
            ${S.snapshots.length ? `
            <table class="vm-table">
                <thead><tr><th>ID</th><th>Nazwa</th><th>Rozmiar</th><th>Data</th><th class="app-col-actions-md">Akcje</th></tr></thead>
                <tbody>${S.snapshots.map(s => `
                    <tr>
                        <td>${esc(s.id)}</td>
                        <td><strong>${esc(s.tag)}</strong></td>
                        <td>${esc(s.vm_size)}</td>
                        <td>${esc(s.date)} ${esc(s.time)}</td>
                        <td>
                            <button class="vm-btn vm-btn-sm vm-btn-success" data-restore="${esc(s.tag)}" title="${t('Przywróć')}"><i class="fas fa-undo"></i></button>
                            <button class="vm-btn vm-btn-sm vm-btn-danger" data-del-snap="${esc(s.tag)}" title="${t('Usuń')}"><i class="fas fa-trash"></i></button>
                        </td>
                    </tr>
                `).join('')}</tbody>
            </table>` : `<div class="vm-empty">${t('Brak snapshotów. Dysk musi być w formacie QCOW2.')}</div>`}
        `;

        dc.querySelector('#vm-snap-create')?.addEventListener('click', async () => {
            const name = await promptDialog(t('Snapshot'), t('Nazwa snapshotu:'));
            if (!name) return;
            try {
                const r = await api(`/vm/machines/${vm.id}/snapshots`, { method: 'POST', body: { name } });
                toast(r.message || 'Snapshot utworzony', 'success');
                renderSnapshotsPanel(dc);
            } catch (e) { toast(e.message || t('Błąd'), 'error'); }
        });

        dc.querySelectorAll('[data-restore]').forEach(btn => {
            btn.addEventListener('click', async () => {
                if (!confirm(`${t('Przywrócić snapshot')} "${btn.dataset.restore}"?`)) return;
                try {
                    const r = await api(`/vm/machines/${vm.id}/snapshots/${encodeURIComponent(btn.dataset.restore)}`, { method: 'POST' });
                    toast(r.message || t('Snapshot przywrócony'), 'success');
                } catch (e) { toast(e.message || t('Błąd'), 'error'); }
            });
        });

        dc.querySelectorAll('[data-del-snap]').forEach(btn => {
            btn.addEventListener('click', async () => {
                if (!confirm(`${t('Usunąć snapshot')} "${btn.dataset.delSnap}"?`)) return;
                try {
                    await api(`/vm/machines/${vm.id}/snapshots/${encodeURIComponent(btn.dataset.delSnap)}`, { method: 'DELETE' });
                    toast(t('Snapshot usunięty'), 'success');
                    renderSnapshotsPanel(dc);
                } catch (e) { toast(e.message || t('Błąd'), 'error'); }
            });
        });
    }

    // Disk panel
    async function renderDiskPanel(dc) {
        const vm = S.selectedVM;
        dc.innerHTML = `<div class="vm-loading"><i class="fas fa-spinner fa-spin"></i> ${t('Ładowanie info o dysku...')}</div>`;
        try {
            S.diskInfo = await api(`/vm/machines/${vm.id}/disk-info`);
        } catch (e) {
            dc.innerHTML = `<div class="vm-empty">${esc(e.message || 'Brak danych o dysku')}</div>`;
            return;
        }
        const d = S.diskInfo;

        dc.innerHTML = `
            <div class="vm-info-card app-modal-lg">
                <h4><i class="fas fa-hdd"></i> Informacje o dysku</h4>
                <div class="vm-info-row"><span>Format:</span><span>${esc(d.format)}</span></div>
                <div class="vm-info-row"><span>Rozmiar wirtualny:</span><span>${esc(d.virtual_size_human)}</span></div>
                <div class="vm-info-row"><span>Rozmiar na dysku:</span><span>${esc(d.actual_size_human)}</span></div>
                <div class="vm-info-row"><span>Plik:</span><span class="vm-mono app-text-xs">${esc(d.filename)}</span></div>
            </div>
            <div class="app-mt-lg">
                <button class="vm-btn vm-btn-primary vm-btn-sm" id="vm-disk-resize"><i class="fas fa-expand-arrows-alt"></i> ${t('Powiększ dysk')}</button>
            </div>
        `;

        dc.querySelector('#vm-disk-resize')?.addEventListener('click', async () => {
            const size = await promptDialog(t('Powiększ dysk'), t('Powiększ o (np. +10G, +512M):'), '+10G');
            if (!size) return;
            try {
                const r = await api(`/vm/machines/${vm.id}/resize-disk`, { method: 'POST', body: { size } });
                toast(r.message || t('Dysk powiększony'), 'success');
                renderDiskPanel(dc);
            } catch (e) { toast(e.message || t('Błąd'), 'error'); }
        });
    }

    // ─── IMAGES TAB ───

    async function loadImages() {
        try { S.images = await api('/vm/images'); } catch { S.images = []; }
    }

    function renderImagesTab() {
        main.innerHTML = `
            <div class="vm-toolbar">
                <span class="vm-toolbar-title"><i class="fas fa-compact-disc"></i> Obrazy ISO/IMG <span class="vm-badge" id="vm-img-cnt">0</span></span>
                <label class="vm-btn vm-btn-primary" id="vm-upload-label">
                    <i class="fas fa-upload"></i> ${t('Prześlij obraz')}
                    <input type="file" id="vm-upload-input" accept=".iso,.img,.raw,.qcow2,.vdi,.vmdk" class="hidden">
                </label>
                <button class="vm-btn" id="vm-img-refresh"><i class="fas fa-sync-alt"></i></button>
            </div>
            <div id="vm-upload-progress" class="vm-upload-bar hidden">
                <div class="vm-upload-fill" id="vm-upload-fill"></div>
                <span id="vm-upload-text">0%</span>
            </div>
            <div class="vm-table-wrap">
                <table class="vm-table">
                    <thead><tr><th>Nazwa</th><th>Typ</th><th>Rozmiar</th><th>Data</th><th class="app-col-actions-xs">Akcje</th></tr></thead>
                    <tbody id="vm-img-tbody"></tbody>
                </table>
            </div>
            <div class="vm-section-divider"><i class="fas fa-hammer"></i> Lokalne obrazy (Builder)</div>
            <div class="vm-table-wrap">
                <table class="vm-table">
                    <thead><tr><th>Nazwa</th><th>Typ</th><th>Rozmiar</th><th>Data</th><th class="app-col-actions-sm">Akcje</th></tr></thead>
                    <tbody id="vm-builder-tbody"></tbody>
                </table>
            </div>
        `;

        main.querySelector('#vm-img-refresh').addEventListener('click', async () => { await loadImages(); fillImagesTable(); await loadBuilderImages(); });
        main.querySelector('#vm-upload-input').addEventListener('change', handleImageUpload);
        loadImages().then(fillImagesTable);
        loadBuilderImages();
    }

    function fillImagesTable() {
        const tbody = main.querySelector('#vm-img-tbody');
        if (!tbody) return;
        const badge = main.querySelector('#vm-img-cnt');
        if (badge) badge.textContent = S.images.length;

        if (!S.images.length) {
            tbody.innerHTML = `<tr><td colspan="5" class="vm-empty-cell">${t('Brak obrazów — prześlij plik ISO lub IMG')}</td></tr>`;
            return;
        }

        tbody.innerHTML = S.images.map(img => `
            <tr>
                <td><i class="fas fa-compact-disc app-pre-icon"></i> ${esc(img.name)}</td>
                <td>${esc(img.type)}</td>
                <td>${esc(img.size_human)}</td>
                <td>${esc(img.modified)}</td>
                <td><button class="vm-btn vm-btn-sm vm-btn-danger" data-del-img="${esc(img.name)}" title="${t('Usuń')}"><i class="fas fa-trash"></i></button></td>
            </tr>
        `).join('');

        tbody.querySelectorAll('[data-del-img]').forEach(btn => {
            btn.addEventListener('click', async () => {
                if (!confirm(`${t('Usunąć obraz')} "${btn.dataset.delImg}"?`)) return;
                try {
                    await api(`/vm/images/${encodeURIComponent(btn.dataset.delImg)}`, { method: 'DELETE' });
                    toast(t('Obraz usunięty'), 'success');
                    await loadImages(); fillImagesTable();
                } catch (e) { toast(e.message || t('Błąd usuwania'), 'error'); }
            });
        });
    }

    async function loadBuilderImages() {
        const tbody = main.querySelector('#vm-builder-tbody');
        if (!tbody) return;
        let imgs = [];
        try { imgs = await api('/vm/builder-images'); } catch { }
        if (!imgs.length) {
            tbody.innerHTML = `<tr><td colspan="5" class="vm-empty-cell">${t('Brak obrazów z Buildera')}</td></tr>`;
            return;
        }
        tbody.innerHTML = imgs.map(img => `
            <tr>
                <td><i class="fas fa-hammer app-pre-icon app-icon-orange"></i> ${esc(img.name)}</td>
                <td>${esc(img.type)}</td>
                <td>${esc(img.size_human)}</td>
                <td>${esc(img.modified)}</td>
                <td>
                    <button class="vm-btn vm-btn-sm vm-btn-primary" data-copy-builder="${esc(img.path)}" data-name="${esc(img.name)}" title="${t('Kopiuj do obrazów VM')}"><i class="fas fa-copy"></i> Kopiuj</button>
                </td>
            </tr>
        `).join('');
        tbody.querySelectorAll('[data-copy-builder]').forEach(btn => {
            btn.addEventListener('click', async () => {
                const name = btn.dataset.name;
                if (!confirm(`${t('Skopiować')} "${name}" ${t('do obrazów VM?')}\n${t('Plik może być duży — to zajmie chwilę.')}`)) return;
                btn.disabled = true;
                btn.innerHTML = '<i class="fas fa-spinner fa-spin"></i> Kopiowanie...';
                try {
                    const r = await api('/vm/builder-images/copy', { method: 'POST', body: { path: btn.dataset.copyBuilder } });
                    toast(r.message || 'Skopiowano', 'success');
                    await loadImages(); fillImagesTable();
                    await loadBuilderImages();
                } catch (e) {
                    toast(e.message || t('Błąd kopiowania'), 'error');
                    btn.disabled = false;
                    btn.innerHTML = '<i class="fas fa-copy"></i> Kopiuj';
                }
            });
        });
    }

    async function handleImageUpload(e) {
        const file = e.target.files[0];
        if (!file) return;
        const form = new FormData();
        form.append('file', file);

        const progressBar = main.querySelector('#vm-upload-progress');
        const fill = main.querySelector('#vm-upload-fill');
        const text = main.querySelector('#vm-upload-text');
        progressBar.style.display = 'block';

        try {
            await new Promise((resolve, reject) => {
                const xhr = new XMLHttpRequest();
                xhr.open('POST', '/api/vm/images');
                xhr.upload.onprogress = (ev) => {
                    if (ev.lengthComputable) {
                        const pct = Math.round((ev.loaded / ev.total) * 100);
                        fill.style.width = pct + '%';
                        text.textContent = pct + '%';
                    }
                };
                xhr.onload = () => {
                    if (xhr.status >= 200 && xhr.status < 300) resolve();
                    else reject(new Error(JSON.parse(xhr.responseText).error || 'Upload failed'));
                };
                xhr.onerror = () => reject(new Error(t('Błąd połączenia')));
                xhr.send(form);
            });
            toast(t('Obraz przesłany'), 'success');
            await loadImages(); fillImagesTable();
        } catch (err) {
            toast(err.message || t('Błąd przesyłania'), 'error');
        }
        progressBar.style.display = 'none';
        fill.style.width = '0%';
        e.target.value = '';
    }

    // ─── SYSTEM TAB ───

    async function renderSystemTab() {
        main.innerHTML = '<div class="vm-loading"><i class="fas fa-spinner fa-spin"></i> Sprawdzanie systemu...</div>';
        try { S.status = await api('/vm/status'); } catch { S.status = { available: false }; }
        const st = S.status;

        main.innerHTML = `
            <div class="vm-sys-grid">
                <div class="vm-info-card">
                    <h4><i class="fas fa-microchip"></i> QEMU</h4>
                    <div class="vm-info-row">
                        <span>Status:</span>
                        <span>${st.available
                            ? '<span class="app-text-ok"><i class="fas fa-check-circle"></i> Zainstalowany</span>'
                            : '<span class="app-icon-danger"><i class="fas fa-times-circle"></i> Nie zainstalowany</span>'}</span>
                    </div>
                </div>
                <div class="vm-info-card">
                    <h4><i class="fas fa-bolt"></i> KVM</h4>
                    <div class="vm-info-row">
                        <span>${t('Akceleracja sprzętowa:')}</span>
                        <span>${st.kvm
                            ? `<span class="app-text-ok"><i class="fas fa-check-circle"></i> ${t('Dostępna')}</span>`
                            : `<span class="app-text-warn"><i class="fas fa-exclamation-triangle"></i> ${t('Niedostępna (QEMU będzie wolniejszy)')}</span>`}</span>
                    </div>
                </div>
            </div>
            ${!st.available ? `<div class="vm-empty app-mt-lg"><i class="fas fa-info-circle"></i> ${t('Zainstaluj paczkę VM Manager w App Store aby korzystać z wirtualizacji.')}</div>` : ''}
        `;
    }

    // ─── INIT ───
    renderTab();

    const refreshInterval = setInterval(() => {
        if (!WM.windows.has('vm-manager')) { clearInterval(refreshInterval); clearAllIntervals(); return; }
        if (S.tab === 'machines' && !S.selectedVM) { loadMachines().then(fillMachinesTable); }
    }, 8000);
    addInterval(refreshInterval);
}


// ═══════════════════════════════════════════════════════════
//  EVENT LOG (Dziennik zdarzeń)
// ═══════════════════════════════════════════════════════════

