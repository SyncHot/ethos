/* ═══════════════════════════════════════════════════════════
   ${t('EthOS — Printer (Serwer Druku + Zarządzanie drukarkami)')}
   Print documents via CUPS + discover/add/remove/manage printers
   ═══════════════════════════════════════════════════════════ */

AppRegistry['printer'] = function (appDef) {
    createWindow('printer', {
        title: t('Serwer druku'),
        icon: appDef.icon,
        iconColor: appDef.color,
        width: 760,
        height: 650,
        onRender: (body) => renderPrinterApp(body),
    });
};

function renderPrinterApp(body) {
    body.innerHTML = `
    <div class="printer-app">
        <!-- Tabs -->
        <div class="printer-tabs">
            <button class="printer-tab active" data-tab="print"><i class="fas fa-print"></i> Drukuj</button>
            <button class="printer-tab" data-tab="manage"><i class="fas fa-cogs"></i> ${t('Zarządzaj drukarkami')}</button>
        </div>

        <!-- === PRINT TAB === -->
        <div class="printer-tab-content active" id="pr-tab-print">
            <div class="printer-status-bar" id="pr-statusbar">
                <i class="fas fa-circle" style="color:var(--text-muted)"></i>
                <span id="pr-status-text">Sprawdzanie drukarki...</span>
                <button class="fm-toolbar-btn btn-sm" id="pr-refresh-status" style="margin-left:auto"><i class="fas fa-sync-alt"></i></button>
            </div>

            <div class="printer-upload-area" id="pr-upload-area">
                <i class="fas fa-cloud-upload-alt" style="font-size:40px;color:var(--accent);margin-bottom:12px"></i>
                <p>${t('Przeciągnij plik tutaj lub kliknij aby wybrać')}</p>
                <p style="font-size:0.8em;color:var(--text-muted)">PDF, DOC, DOCX, XLS, XLSX, ODS, ODT, JPG, PNG, TXT</p>
                <input type="file" id="pr-file-input" style="display:none"
                    accept=".pdf,.doc,.docx,.xls,.xlsx,.ods,.odt,.jpg,.jpeg,.png,.gif,.bmp,.tiff,.txt,.rtf">
            </div>
            <div class="printer-selected hidden" id="pr-selected">
                <i class="fas fa-file"></i>
                <span id="pr-filename">—</span>
                <button class="fm-toolbar-btn btn-sm" id="pr-clear-file"><i class="fas fa-times"></i></button>
            </div>

            <div class="printer-settings">
                <div class="printer-form-row">
                    <label>Drukarka:</label>
                    <select id="pr-printer" class="fm-input"><option value="">${t('Ładowanie...')}</option></select>
                </div>
                <div class="printer-form-row">
                    <label>Kopie:</label>
                    <input type="number" id="pr-copies" value="1" min="1" max="99" class="fm-input" style="width:80px">
                    <label class="storage-check"><input type="checkbox" id="pr-duplex"> Dwustronne</label>
                </div>
                <div class="printer-form-row">
                    <label>Rozmiar:</label>
                    <select id="pr-pagesize" class="fm-input">
                        <option value="A4">A4</option><option value="A3">A3</option>
                        <option value="A5">A5</option><option value="Letter">Letter</option>
                    </select>
                    <label>Orientacja:</label>
                    <select id="pr-orientation" class="fm-input">
                        <option value="portrait">Portret</option><option value="landscape">Krajobraz</option>
                    </select>
                </div>
            </div>

            <button class="printer-btn" id="pr-print-btn" disabled>
                <i class="fas fa-print"></i> Drukuj
            </button>

            <div class="printer-result hidden" id="pr-result"></div>

            <div class="printer-jobs" style="margin-top:16px">
                <h3 style="margin-bottom:8px"><i class="fas fa-list"></i> Kolejka drukowania</h3>
                <div id="pr-jobs">${t('Ładowanie...')}</div>
            </div>
        </div>

        <!-- === MANAGE TAB === -->
        <div class="printer-tab-content" id="pr-tab-manage">

            <!-- Installed printers -->
            <div class="pm-section">
                <div class="pm-section-header">
                    <h3><i class="fas fa-print"></i> Zainstalowane drukarki</h3>
                    <button class="fm-toolbar-btn btn-sm" id="pm-refresh-installed"><i class="fas fa-sync-alt"></i></button>
                </div>
                <div id="pm-installed-list" class="pm-list">${t('Ładowanie...')}</div>
            </div>

            <!-- Discover -->
            <div class="pm-section">
                <div class="pm-section-header">
                    <h3><i class="fas fa-search"></i> Wykryj drukarki w sieci</h3>
                    <button class="fm-toolbar-btn btn-sm" id="pm-scan-btn"><i class="fas fa-radar"></i> Skanuj</button>
                </div>
                <div id="pm-discover-list" class="pm-list">
                    <p class="pm-muted">${t('Kliknij „Skanuj" aby wyszukać drukarki w sieci lokalnej.')}</p>
                </div>
            </div>

            <!-- Add manually -->
            <div class="pm-section">
                <div class="pm-section-header">
                    <h3><i class="fas fa-plus-circle"></i> ${t('Dodaj drukarkę ręcznie')}</h3>
                    <button class="fm-toolbar-btn btn-sm" id="pm-toggle-manual"><i class="fas fa-chevron-down"></i></button>
                </div>
                <div id="pm-manual-form" class="pm-manual hidden">
                    <div class="printer-form-row">
                        <label>Nazwa:</label>
                        <input id="pm-add-name" class="fm-input" placeholder="np. Samsung_M2835DW" style="flex:1">
                    </div>
                    <div class="printer-form-row">
                        <label>URI:</label>
                        <input id="pm-add-uri" class="fm-input" placeholder="socket://192.168.1.100:9100" style="flex:1">
                    </div>
                    <div class="printer-form-row">
                        <label>Sterownik (PPD):</label>
                        <select id="pm-add-ppd" class="fm-input" style="flex:1">
                            <option value="">Automatyczny (IPP Everywhere)</option>
                        </select>
                        <button class="fm-toolbar-btn btn-sm" id="pm-load-drivers" title="${t('Załaduj sterowniki')}"><i class="fas fa-download"></i></button>
                    </div>
                    <div class="printer-form-row">
                        <label>Opis:</label>
                        <input id="pm-add-info" class="fm-input" placeholder="Opcjonalny opis" style="flex:1">
                    </div>
                    <div class="printer-form-row">
                        <label class="storage-check"><input type="checkbox" id="pm-add-default"> ${t('Ustaw jako domyślną')}</label>
                        <label class="storage-check"><input type="checkbox" id="pm-add-shared" checked> ${t('Udostępnij w sieci')}</label>
                    </div>
                    <button class="printer-btn" id="pm-add-btn" style="margin-top:8px">
                        <i class="fas fa-plus"></i> ${t('Dodaj drukarkę')}
                    </button>
                </div>
            </div>
        </div>
    </div>`;

    const $ = (sel) => body.querySelector(sel);
    const $$ = (sel) => body.querySelectorAll(sel);

    /* ── Tab switching ── */
    $$('.printer-tab').forEach(tab => {
        tab.onclick = () => {
            $$('.printer-tab').forEach(t => t.classList.remove('active'));
            $$('.printer-tab-content').forEach(c => c.classList.remove('active'));
            tab.classList.add('active');
            $(`#pr-tab-${tab.dataset.tab}`).classList.add('active');
            if (tab.dataset.tab === 'manage') loadInstalled();
        };
    });

    /* ══════════════════════════════════════
       PRINT TAB logic
       ══════════════════════════════════════ */
    let selectedFile = null;
    const uploadArea = $('#pr-upload-area');
    const fileInput = $('#pr-file-input');

    uploadArea.onclick = () => fileInput.click();
    uploadArea.ondragover = e => { e.preventDefault(); uploadArea.classList.add('dragover'); };
    uploadArea.ondragleave = () => uploadArea.classList.remove('dragover');
    uploadArea.ondrop = e => {
        e.preventDefault();
        uploadArea.classList.remove('dragover');
        if (e.dataTransfer.files.length) selectFile(e.dataTransfer.files[0]);
    };
    fileInput.onchange = () => { if (fileInput.files.length) selectFile(fileInput.files[0]); };

    function selectFile(file) {
        selectedFile = file;
        $('#pr-filename').textContent = `${file.name} (${(file.size / 1024).toFixed(1)} KB)`;
        $('#pr-selected').classList.remove('hidden');
        uploadArea.classList.add('hidden');
        $('#pr-print-btn').disabled = false;
    }

    $('#pr-clear-file').onclick = () => {
        selectedFile = null;
        $('#pr-selected').classList.add('hidden');
        uploadArea.classList.remove('hidden');
        $('#pr-print-btn').disabled = true;
        fileInput.value = '';
    };

    async function loadPrinters() {
        try {
            const data = await api('/printer/printers');
            const sel = $('#pr-printer');
            sel.innerHTML = (data.printers || []).map(p =>
                `<option value="${p.name}">${p.name} ${p.is_default ? t('(domyślna)') : ''}</option>`
            ).join('') || '<option value="">Brak drukarek</option>';
        } catch (e) {
            $('#pr-printer').innerHTML = `<option value="">${t('Błąd ładowania')}</option>`;
        }
    }

    async function checkStatus() {
        try {
            const data = await api('/printer/status');
            const icon = $('#pr-statusbar').querySelector('i');
            const text = $('#pr-status-text');
            if (data.printer_online) {
                icon.style.color = '#2dd4a8';
                text.textContent = `Drukarka online: ${data.printer_name || '—'}`;
            } else if (data.count > 0) {
                icon.style.color = '#ffb347';
                text.textContent = `${data.count} drukark(i) — wszystkie offline`;
            } else {
                icon.style.color = '#ff4d6a';
                text.textContent = t('Brak drukarek — dodaj w „Zarządzaj"');
            }
        } catch (e) {
            $('#pr-status-text').textContent = t('Brak połączenia z CUPS');
        }
    }

    async function loadJobs() {
        try {
            const data = await api('/printer/jobs');
            const jobs = data.jobs || [];
            $('#pr-jobs').innerHTML = jobs.length ? jobs.map(j => `
                <div class="printer-job">
                    <span><i class="fas fa-file"></i> ${j.id || 'Zadanie'}</span>
                    <span class="fm-badge">${j.status || '—'}</span>
                    <span style="color:var(--text-muted);font-size:0.85em">${j.user || ''}</span>
                    ${j.id ? `<button class="fm-toolbar-btn btn-red btn-sm" onclick="cancelPrintJob(this, '${j.id}')"><i class="fas fa-times"></i></button>` : ''}
                </div>
            `).join('') : `<p style="color:var(--text-muted)">${t('Brak zadań')}</p>`;
        } catch (e) {
            $('#pr-jobs').innerHTML = `<p style="color:var(--text-muted)">${t('Błąd ładowania')}</p>`;
        }
    }

    $('#pr-print-btn').onclick = async () => {
        if (!selectedFile) return;
        const btn = $('#pr-print-btn');
        btn.disabled = true;
        btn.innerHTML = '<i class="fas fa-spinner fa-spin"></i> Drukowanie...';
        const resultEl = $('#pr-result');

        try {
            const formData = new FormData();
            formData.append('file', selectedFile);
            formData.append('printer', $('#pr-printer').value);
            formData.append('copies', $('#pr-copies').value);
            formData.append('duplex', $('#pr-duplex').checked ? 'true' : 'false');
            formData.append('page_size', $('#pr-pagesize').value);
            formData.append('orientation', $('#pr-orientation').value);

            const headers = {};
            if (NAS.token) headers['Authorization'] = `Bearer ${NAS.token}`;
            if (NAS.csrfToken) headers['X-CSRFToken'] = NAS.csrfToken;
            const resp = await fetch('/api/printer/print', { method: 'POST', headers, body: formData });
            const data = await resp.json();

            if (data.success) {
                resultEl.className = 'printer-result success';
                resultEl.innerHTML = `<i class="fas fa-check-circle"></i> ${data.message || t('Wysłano do druku')}`;
            } else {
                resultEl.className = 'printer-result error';
                resultEl.innerHTML = `<i class="fas fa-exclamation-circle"></i> ${data.error || t('Błąd drukowania')}`;
            }
            resultEl.classList.remove('hidden');
            loadJobs();
        } catch (e) {
            resultEl.className = 'printer-result error';
            resultEl.innerHTML = `<i class="fas fa-exclamation-circle"></i> ${t('Błąd:')} ${e.message}`;
            resultEl.classList.remove('hidden');
        } finally {
            btn.disabled = false;
            btn.innerHTML = '<i class="fas fa-print"></i> Drukuj';
        }
    };

    window.cancelPrintJob = async (btn, jobId) => {
        try {
            await api(`/printer/cancel/${jobId}`, { method: 'POST' });
            toast(t('Anulowano'), 'success');
            loadJobs();
        } catch (e) { toast(t('Błąd anulowania'), 'error'); }
    };

    $('#pr-refresh-status').onclick = () => { checkStatus(); loadJobs(); loadPrinters(); };

    /* ══════════════════════════════════════
       MANAGE TAB logic
       ══════════════════════════════════════ */

    /* -- Installed printers -- */
    async function loadInstalled() {
        const el = $('#pm-installed-list');
        el.innerHTML = `<p class="pm-muted"><i class="fas fa-spinner fa-spin"></i> ${t('Ładowanie...')}</p>`;
        try {
            const data = await api('/printer/printers');
            const printers = data.printers || [];
            if (!printers.length) {
                el.innerHTML = `<p class="pm-muted">${t('Brak zainstalowanych drukarek.')}</p>`;
                return;
            }
            el.innerHTML = printers.map(p => {
                const statusColor = p.reachable ? '#2dd4a8' : (p.reachable === false ? '#ff4d6a' : '#888');
                const statusLabel = p.reachable ? 'Online' : (p.reachable === false ? 'Offline' : '—');
                const stateLabel = p.status === 'idle' ? 'Gotowa' : (p.status === 'printing' ? 'Drukuje' : t('Wyłączona'));
                return `
                <div class="pm-card">
                    <div class="pm-card-main">
                        <div class="pm-card-icon">
                            <i class="fas fa-print" style="color:${statusColor}"></i>
                        </div>
                        <div class="pm-card-info">
                            <div class="pm-card-name">${p.name} ${p.is_default ? `<span class="fm-badge" style="background:var(--accent);color:#fff;font-size:0.7em">${t('domyślna')}</span>` : ''}</div>
                            <div class="pm-card-detail">${p.uri || '—'}</div>
                            <div class="pm-card-detail">
                                <span style="color:${statusColor}">${statusLabel}</span> · ${stateLabel}
                                ${p.ip ? ` · IP: ${p.ip}` : ''}
                            </div>
                        </div>
                    </div>
                    <div class="pm-card-actions">
                        <button class="fm-toolbar-btn btn-sm" title="${t('Obudź')}" data-action="wake" data-name="${p.name}"><i class="fas fa-bolt"></i></button>
                        ${p.status === 'disabled'
                            ? `<button class="fm-toolbar-btn btn-sm" title="${t('Włącz')}" data-action="enable" data-name="${p.name}"><i class="fas fa-toggle-on"></i></button>`
                            : `<button class="fm-toolbar-btn btn-sm" title="${t('Wyłącz')}" data-action="disable" data-name="${p.name}"><i class="fas fa-toggle-off"></i></button>`
                        }
                        ${!p.is_default ? `<button class="fm-toolbar-btn btn-sm" title="${t('Ustaw domyślną')}" data-action="default" data-name="${p.name}"><i class="fas fa-star"></i></button>` : ''}
                        <button class="fm-toolbar-btn btn-sm btn-red" title="${t('Usuń')}" data-action="remove" data-name="${p.name}"><i class="fas fa-trash"></i></button>
                    </div>
                </div>`;
            }).join('');

            // Bind card actions
            el.querySelectorAll('[data-action]').forEach(btn => {
                btn.onclick = () => handlePrinterAction(btn.dataset.action, btn.dataset.name);
            });
        } catch (e) {
            el.innerHTML = `<p class="pm-muted" style="color:#ff4d6a">${t('Błąd:')} ${e.message}</p>`;
        }
    }

    async function handlePrinterAction(action, name) {
        try {
            if (action === 'remove') {
                if (!await confirmDialog(t('Usunąć drukarkę') + ' "' + name + '"?')) return;
            }
            await api(`/printer/${action}`, { method: 'POST', body: { name, printer: name } });
            toast(action === 'remove' ? t('Usunięto') : action === 'wake' ? t('Wybudzono') : 'OK', 'success');
            loadInstalled();
            loadPrinters();
            checkStatus();
        } catch (e) {
            toast(`${t('Błąd:')} ${e.message}`, 'error');
        }
    }

    $('#pm-refresh-installed').onclick = loadInstalled;

    /* -- Discovery -- */
    $('#pm-scan-btn').onclick = async () => {
        const el = $('#pm-discover-list');
        const btn = $('#pm-scan-btn');
        btn.disabled = true;
        btn.innerHTML = '<i class="fas fa-spinner fa-spin"></i> Skanowanie...';
        el.innerHTML = `<p class="pm-muted"><i class="fas fa-spinner fa-spin"></i> ${t('Skanowanie sieci (może potrwać do 15s)...')}</p>`;
        try {
            const data = await api('/printer/discover');
            const printers = (data.printers || []).filter(p => !p.installed);
            if (!printers.length) {
                el.innerHTML = `<p class="pm-muted">${t('Nie znaleziono nowych drukarek. Spróbuj dodać ręcznie.')}</p>`;
                return;
            }
            el.innerHTML = printers.map((p, i) => `
                <div class="pm-discover-row">
                    <div class="pm-discover-info">
                        <i class="fas fa-print" style="color:var(--accent)"></i>
                        <div>
                            <div style="font-weight:600">${p.info || p.uri}</div>
                            <div class="pm-card-detail">${p.uri}</div>
                            ${p.ip ? `<div class="pm-card-detail">IP: ${p.ip} · ${p.protocol}</div>` : `<div class="pm-card-detail">${p.protocol}</div>`}
                        </div>
                    </div>
                    <button class="fm-toolbar-btn btn-sm" title="Dodaj" data-idx="${i}"><i class="fas fa-plus"></i> Dodaj</button>
                </div>
            `).join('');

            el.querySelectorAll('[data-idx]').forEach(btn => {
                btn.onclick = () => {
                    const p = printers[parseInt(btn.dataset.idx)];
                    quickAddPrinter(p);
                };
            });
        } catch (e) {
            el.innerHTML = `<p class="pm-muted" style="color:#ff4d6a">${t('Błąd:')} ${e.message}</p>`;
        } finally {
            btn.disabled = false;
            btn.innerHTML = '<i class="fas fa-satellite-dish"></i> Skanuj';
        }
    };

    async function quickAddPrinter(p) {
        const suggestedName = (p.info || p.uri).replace(/[^A-Za-z0-9]/g, '_').substring(0, 30);
        const name = prompt('Nazwa drukarki:', suggestedName);
        if (!name) return;
        try {
            const res = await api('/printer/add', {
                method: 'POST',
                body: { name, uri: p.uri, set_default: false, shared: true },
            });
            if (res.success) {
                toast(`Dodano: ${name}`, 'success');
                loadInstalled();
                loadPrinters();
                checkStatus();
                // Re-scan to update "installed" flags
                $('#pm-scan-btn').onclick();
            } else {
                toast(res.error || t('Błąd dodawania'), 'error');
            }
        } catch (e) {
            toast(`${t('Błąd:')} ${e.message}`, 'error');
        }
    }

    /* -- Manual add form -- */
    $('#pm-toggle-manual').onclick = () => {
        const form = $('#pm-manual-form');
        form.classList.toggle('hidden');
        const icon = $('#pm-toggle-manual').querySelector('i');
        icon.className = form.classList.contains('hidden') ? 'fas fa-chevron-down' : 'fas fa-chevron-up';
    };

    $('#pm-load-drivers').onclick = async () => {
        const sel = $('#pm-add-ppd');
        sel.innerHTML = `<option value="">${t('Ładowanie sterowników...')}</option>`;
        try {
            const data = await api('/printer/drivers');
            const drivers = data.drivers || [];
            sel.innerHTML = '<option value="">Automatyczny (IPP Everywhere)</option>'
                + drivers.map(d => `<option value="${d.ppd}">${d.description}</option>`).join('');
            toast(`${t('Załadowano')} ${drivers.length} ${t('sterowników')}`, 'success');
        } catch (e) {
            sel.innerHTML = '<option value="">Automatyczny (IPP Everywhere)</option>';
            toast(t('Błąd ładowania sterowników'), 'error');
        }
    };

    $('#pm-add-btn').onclick = async () => {
        const name = $('#pm-add-name').value.trim();
        const uri = $('#pm-add-uri').value.trim();
        if (!name || !uri) {
            toast(t('Podaj nazwę i URI drukarki'), 'error');
            return;
        }
        const btn = $('#pm-add-btn');
        btn.disabled = true;
        btn.innerHTML = '<i class="fas fa-spinner fa-spin"></i> Dodawanie...';
        try {
            const res = await api('/printer/add', {
                method: 'POST',
                body: {
                    name,
                    uri,
                    ppd: $('#pm-add-ppd').value || null,
                    info: $('#pm-add-info').value.trim() || null,
                    set_default: $('#pm-add-default').checked,
                    shared: $('#pm-add-shared').checked,
                },
            });
            if (res.success) {
                toast(`Dodano: ${res.name}`, 'success');
                $('#pm-add-name').value = '';
                $('#pm-add-uri').value = '';
                $('#pm-add-info').value = '';
                loadInstalled();
                loadPrinters();
                checkStatus();
            } else {
                toast(res.error || t('Błąd'), 'error');
            }
        } catch (e) {
            toast(`${t('Błąd:')} ${e.message}`, 'error');
        } finally {
            btn.disabled = false;
            btn.innerHTML = `<i class="fas fa-plus"></i> ${t('Dodaj drukarkę')}`;
        }
    };

    /* ── Initial load ── */
    loadPrinters();
    checkStatus();
    loadJobs();
}
