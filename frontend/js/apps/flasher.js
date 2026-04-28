/* ═══════════════════════════════════════════════════════════
   EthOS — USB Flasher  (Kreator bootowalnego USB)
   Flash ISO/IMG images to USB drives.
   ═══════════════════════════════════════════════════════════ */

AppRegistry['usb-flasher'] = function (appDef) {
    const _cl = (level, msg, details) => typeof NAS !== 'undefined' && NAS.logClient
        ? NAS.logClient('usb-flasher', level, msg, details) : console.log('[usb-flasher]', msg, details || '');

    createWindow('usb-flasher', {
        title: t('Kreator USB'),
        icon: appDef.icon,
        iconColor: appDef.color,
        width: 640,
        height: 540,
        onRender: (body) => renderFlasherApp(body),
    });
};

function renderFlasherApp(body) {
    const state = { images: [], drives: [], selectedImage: null, selectedDisk: null, flashing: false };

    body.innerHTML = `
    <style>
        .fl-wrap { display:flex; flex-direction:column; height:100%; padding:16px; gap:12px; overflow-y:auto; box-sizing:border-box; }
        .fl-section { background:var(--bg-card, var(--bg-secondary)); border-radius:10px; padding:14px 16px; flex-shrink:0; }
        .fl-section-title { font-weight:600; font-size:13px; margin-bottom:10px; display:flex; align-items:center; gap:8px; color:var(--text-primary); }
        .fl-section-title i { width:18px; text-align:center; }

        .fl-select-wrap { display:flex; gap:8px; align-items:center; }
        .fl-select { flex:1; background:var(--bg-primary); border:1px solid var(--border); border-radius:6px; padding:8px 10px; color:var(--text-primary); font-size:13px; min-width:0; }
        .fl-select option { background:var(--bg-primary); color:var(--text-primary); }
        .fl-btn { background:var(--accent); color:#fff; border:none; border-radius:6px; padding:8px 14px; cursor:pointer; font-size:13px; display:inline-flex; align-items:center; gap:6px; white-space:nowrap; }
        .fl-btn:hover { filter:brightness(1.1); }
        .fl-btn:disabled { opacity:.5; cursor:not-allowed; filter:none; }
        .fl-btn-sm { padding:6px 10px; font-size:12px; }
        .fl-btn-danger { background:#ef4444; }
        .fl-btn-danger:hover { background:#dc2626; }
        .fl-btn-outline { background:transparent; border:1px solid var(--border); color:var(--text-secondary); }
        .fl-btn-outline:hover { border-color:var(--accent); color:var(--accent); }

        .fl-image-info { display:flex; gap:12px; align-items:center; margin-top:10px; padding:10px; background:var(--bg-primary); border-radius:8px; font-size:12px; color:var(--text-muted); }
        .fl-image-info i { font-size:22px; color:var(--accent); flex-shrink:0; }
        .fl-image-name { font-weight:600; color:var(--text-primary); font-size:13px; }
        .fl-image-size { color:var(--text-muted); }

        .fl-drive-info { display:flex; gap:12px; align-items:center; margin-top:10px; padding:10px; background:var(--bg-primary); border-radius:8px; font-size:12px; color:var(--text-muted); }
        .fl-drive-info i { font-size:22px; color:#f59e0b; flex-shrink:0; }
        .fl-drive-warn { color:#f59e0b; font-size:11px; margin-top:6px; display:flex; align-items:center; gap:6px; }

        .fl-actions-row { display:flex; align-items:center; justify-content:center; gap:12px; flex-wrap:wrap; }

        .fl-progress-section { display:none; }
        .fl-progress-section.active { display:block; }
        .fl-progress-bar-outer { height:28px; background:var(--bg-primary); border-radius:14px; overflow:hidden; position:relative; border:1px solid var(--border); }
        .fl-progress-bar-inner { height:100%; background: linear-gradient(90deg, var(--accent), #6366f1); border-radius:14px; transition:width .5s ease; width:0%; }
        .fl-progress-text { position:absolute; inset:0; display:flex; align-items:center; justify-content:center; font-size:13px; font-weight:700; color:#fff; text-shadow:0 1px 3px rgba(0,0,0,.5); }
        .fl-progress-stats { display:flex; justify-content:space-between; align-items:center; font-size:12px; color:var(--text-muted); margin-top:8px; padding:0 4px; gap:8px; }
        .fl-progress-stats span { white-space:nowrap; }
        .fl-progress-msg { text-align:center; font-size:12px; color:var(--text-secondary); margin-top:6px; font-weight:500; }

        .fl-log-section { display:none; }
        .fl-log-section.active { display:block; }
        .fl-log { max-height:120px; overflow-y:auto; background:var(--bg-primary); border-radius:8px; padding:8px 10px; font-family:monospace; font-size:11px; color:var(--text-muted); border:1px solid var(--border); }
        .fl-log-line { padding:2px 0; border-bottom:1px solid var(--border); }
        .fl-log-line:last-child { border:none; }
        .fl-log-line.error { color:#ef4444; }
        .fl-log-line.success { color:#10b981; }

        .fl-result { text-align:center; padding:16px; }
        .fl-result-icon { font-size:44px; margin-bottom:10px; }
        .fl-result-msg { font-size:14px; font-weight:600; }
        .fl-result-detail { font-size:12px; color:var(--text-muted); margin-top:4px; }

        .fl-or { text-align:center; font-size:11px; color:var(--text-muted); margin:6px 0; }
        .fl-browse-row { display:flex; gap:8px; align-items:center; }
        .fl-path-input { flex:1; background:var(--bg-primary); border:1px solid var(--border); border-radius:6px; padding:8px 10px; color:var(--text-primary); font-size:12px; font-family:monospace; min-width:0; }
    </style>

    <div class="fl-wrap">
        <!-- Step 1: Image selection -->
        <div class="fl-section">
            <div class="fl-section-title"><i class="fas fa-compact-disc"></i> Obraz ISO / IMG</div>
            <div class="fl-select-wrap">
                <select class="fl-select" id="fl-image-select">
                    <option value="">— ${t('Szukanie obrazów...')}</option>
                </select>
                <button class="fl-btn fl-btn-sm fl-btn-outline" id="fl-image-refresh" title="${t('Odśwież')}"><i class="fas fa-sync-alt"></i></button>
            </div>
            <div class="fl-or">${t('lub podaj ścieżkę ręcznie')}</div>
            <div class="fl-browse-row">
                <input type="text" class="fl-path-input" id="fl-image-path" placeholder="/home/user/obraz.iso">
                <button class="fl-btn fl-btn-sm fl-btn-outline" id="fl-image-check">${t('Sprawdź')}</button>
            </div>
            <div id="fl-image-info" style="display:none"></div>
            <div id="fl-checksum" style="display:none"></div>
        </div>

        <!-- Step 2: Drive selection -->
        <div class="fl-section">
            <div class="fl-section-title"><i class="fas fa-usb"></i> Dysk docelowy USB</div>
            <div class="fl-select-wrap">
                <select class="fl-select" id="fl-drive-select">
                    <option value="">— ${t('Ładowanie dysków...')}</option>
                </select>
                <button class="fl-btn fl-btn-sm fl-btn-outline" id="fl-drive-refresh" title="${t('Odśwież')}"><i class="fas fa-sync-alt"></i></button>
                <button class="fl-btn fl-btn-sm fl-btn-outline" id="fl-drive-format" title="Formatuj"><i class="fas fa-eraser"></i></button>
            </div>
            <div id="fl-drive-info" style="display:none"></div>
        </div>

        <!-- Step 3: Flash button + options -->
        <div class="fl-section">
            <div class="fl-actions-row">
                <label style="display:inline-flex;align-items:center;gap:6px;font-size:12px;color:var(--text-muted);cursor:pointer">
                    <input type="checkbox" id="fl-verify-check"> Weryfikuj po zapisie
                </label>
                <button class="fl-btn fl-btn-danger" id="fl-flash-btn" style="font-size:14px;padding:10px 24px;" disabled>
                    <i class="fas fa-bolt"></i> Flashuj na USB
                </button>
            </div>
        </div>

        <!-- Progress section (shown during flash) -->
        <div class="fl-section fl-progress-section" id="fl-progress">
            <div class="fl-section-title"><i class="fas fa-spinner fa-spin"></i> ${t('Postęp flashowania')}</div>
            <div class="fl-progress-bar-outer">
                <div class="fl-progress-bar-inner" id="fl-progress-bar"></div>
                <div class="fl-progress-text" id="fl-progress-text">0%</div>
            </div>
            <div class="fl-progress-stats">
                <span id="fl-progress-written">—</span>
                <span id="fl-progress-speed">—</span>
                <span id="fl-progress-eta">—</span>
                <span id="fl-progress-elapsed">—</span>
            </div>
            <div class="fl-progress-msg" id="fl-progress-msg"></div>
            <div style="text-align:center;margin-top:8px">
                <button class="fl-btn fl-btn-sm fl-btn-outline" id="fl-cancel-btn" style="display:none">
                    <i class="fas fa-stop"></i> Anuluj
                </button>
            </div>
        </div>

        <!-- Log section -->
        <div class="fl-section fl-log-section" id="fl-log-section">
            <div class="fl-section-title"><i class="fas fa-terminal"></i> Log</div>
            <div class="fl-log" id="fl-log"></div>
        </div>

        <!-- Result -->
        <div id="fl-result" style="display:none"></div>

        <!-- History -->
        <div class="fl-section" style="margin-top:0">
            <div class="fl-section-title" style="cursor:pointer" id="fl-history-toggle">
                <i class="fas fa-clock-rotate-left"></i> Historia
                <span style="margin-left:auto;font-size:11px;color:var(--text-muted)" id="fl-history-count"></span>
            </div>
            <div id="fl-history-list" style="display:none;max-height:200px;overflow-y:auto"></div>
        </div>
    </div>`;

    const $  = s => body.querySelector(s);
    const $$ = s => body.querySelectorAll(s);

    const imgSelect  = $('#fl-image-select');
    const imgPath    = $('#fl-image-path');
    const driveSelect= $('#fl-drive-select');
    const flashBtn   = $('#fl-flash-btn');
    const progressW  = $('#fl-progress');
    const progressBar= $('#fl-progress-bar');
    const progressTxt= $('#fl-progress-text');
    const progressMsg= $('#fl-progress-msg');
    const progressWritten = $('#fl-progress-written');
    const progressSpeed   = $('#fl-progress-speed');
    const progressEta     = $('#fl-progress-eta');
    const progressElapsed = $('#fl-progress-elapsed');
    const logSection = $('#fl-log-section');
    const logEl      = $('#fl-log');
    const resultEl   = $('#fl-result');
    const imgInfo    = $('#fl-image-info');
    const driveInfo  = $('#fl-drive-info');
    const cancelBtn  = $('#fl-cancel-btn');
    const verifyCheck= $('#fl-verify-check');
    const checksumEl = $('#fl-checksum');

    /* ─── Helpers ─── */
    function humanSize(b) {
        if (b >= 1e12) return (b / 1e12).toFixed(1) + ' TB';
        if (b >= 1e9)  return (b / 1e9).toFixed(1) + ' GB';
        if (b >= 1e6)  return (b / 1e6).toFixed(1) + ' MB';
        return (b / 1024).toFixed(0) + ' KB';
    }

    function addLog(msg, cls = '') {
        logSection.classList.add('active');
        logEl.innerHTML += `<div class="fl-log-line ${cls}">${msg}</div>`;
        logEl.scrollTop = logEl.scrollHeight;
    }

    function updateFlashBtn() {
        flashBtn.disabled = !state.selectedImage || !state.selectedDisk || state.flashing;
    }

    /* ─── Load images ─── */
    async function loadImages() {
        imgSelect.innerHTML = `<option value="">${t('— Szukanie obrazów...')}</option>`;
        try {
            const data = await api('/flasher/images');
            state.images = data.images || [];
            imgSelect.innerHTML = '<option value="">— Wybierz obraz —</option>';
            if (state.images.length === 0) {
                imgSelect.innerHTML = `<option value="">${t('Nie znaleziono obrazów ISO/IMG')}</option>`;
            }
            for (const img of state.images) {
                const opt = document.createElement('option');
                opt.value = img.path;
                opt.textContent = `${img.name}  (${humanSize(img.size)})`;
                opt.title = img.display;
                imgSelect.appendChild(opt);
            }
        } catch (e) {
            imgSelect.innerHTML = `<option value="">${t('Błąd ładowania')}</option>`;
        }
    }

    /* ─── Load drives ─── */
    async function loadDrives() {
        try {
            const data = await api('/flasher/drives');
            const newDrives = data.drives || [];
            // Skip rebuild if drive list unchanged
            const oldKey = state.drives.map(d => d.name + d.size).join(',');
            const newKey = newDrives.map(d => d.name + d.size).join(',');
            if (oldKey === newKey && state.drives.length > 0) return;

            state.drives = newDrives;
            const prevVal = driveSelect.value;
            driveSelect.innerHTML = '<option value="">— Wybierz dysk USB —</option>';
            if (state.drives.length === 0) {
                driveSelect.innerHTML = `<option value="">${t('Nie znaleziono dysków USB')}</option>`;
            }
            for (const d of state.drives) {
                const opt = document.createElement('option');
                opt.value = d.name;
                const label = d.model || d.label || d.name;
                opt.textContent = `/dev/${d.name} — ${label} (${d.size})`;
                driveSelect.appendChild(opt);
            }
            // Restore previous selection if still available
            if (prevVal && state.drives.some(d => d.name === prevVal)) {
                driveSelect.value = prevVal;
            }
        } catch (e) {
            driveSelect.innerHTML = `<option value="">${t('Błąd ładowania')}</option>`;
        }
    }

    /* ─── Image selected ─── */
    imgSelect.onchange = () => {
        const path = imgSelect.value;
        imgPath.value = '';
        if (path) {
            const img = state.images.find(i => i.path === path);
            showImageInfo(img);
            state.selectedImage = path;
        } else {
            imgInfo.style.display = 'none';
            state.selectedImage = null;
        }
        updateFlashBtn();
    };

    /* ─── Manual path check ─── */
    $('#fl-image-check').onclick = async () => {
        const raw = imgPath.value.trim();
        if (!raw) { toast(t('Podaj ścieżkę do obrazu'), 'warning'); return; }

        // Convert user-visible path to container path
        let containerPath = raw;

        try {
            const data = await api(`/flasher/verify?path=${encodeURIComponent(containerPath)}`);
            if (data.valid) {
                imgSelect.value = '';
                state.selectedImage = containerPath;
                showImageInfo({ name: raw.split('/').pop(), size: data.size, path: containerPath, display: raw, compressed: data.compressed });
                toast('Obraz poprawny', 'success');
            } else {
                toast(data.error || t('Nieprawidłowy plik'), 'error');
                state.selectedImage = null;
            }
        } catch (e) {
            toast(t('Plik nie istnieje lub jest niedostępny'), 'error');
            state.selectedImage = null;
        }
        updateFlashBtn();
    };

    function showImageInfo(img) {
        if (!img) { imgInfo.style.display = 'none'; checksumEl.style.display = 'none'; return; }
        const typeLabels = { 'iso9660': 'ISO 9660', 'uefi-img': 'UEFI Image', 'mbr-img': 'MBR Image', 'raw-img': 'Raw Image' };
        let badge = '';
        if (img.compressed) {
            badge = `<span style="background:#6366f1;color:#fff;padding:2px 6px;border-radius:4px;font-size:10px;margin-left:6px">${img.compressed.toUpperCase()}</span>`;
        }
        imgInfo.style.display = '';
        imgInfo.innerHTML = `
            <div class="fl-image-info">
                <i class="fas fa-compact-disc"></i>
                <div style="flex:1;min-width:0">
                    <div class="fl-image-name">${img.name}${badge}</div>
                    <div class="fl-image-size">${humanSize(img.size)}${img.type ? ' — ' + (typeLabels[img.type] || img.type) : ''}</div>
                    <div style="font-size:11px;color:var(--text-muted);margin-top:2px;word-break:break-all">${img.display || img.path}</div>
                </div>
                <button class="fl-btn fl-btn-sm fl-btn-outline" onclick="return false" id="fl-checksum-btn" title="Oblicz SHA256" style="flex-shrink:0"><i class="fas fa-fingerprint"></i> SHA256</button>
            </div>`;
        $('#fl-checksum-btn').onclick = () => showChecksum(img.path);
        checksumEl.style.display = 'none';
        checksumEl.innerHTML = '';
    }

    function showChecksum(path) {
        checksumEl.style.display = '';
        checksumEl.innerHTML = '<div style="font-size:11px;color:var(--text-muted);padding:6px 10px"><i class="fas fa-spinner fa-spin"></i> Obliczanie SHA256...</div>';
        api('/flasher/checksum', { method: 'POST', body: { path } })
            .then(r => {
                checksumEl.innerHTML = `<div style="font-size:11px;color:var(--text-muted);padding:6px 10px;word-break:break-all"><i class="fas fa-fingerprint" style="color:var(--accent)"></i> SHA256: <code style="font-size:10px">${r.sha256}</code></div>`;
            })
            .catch(e => {
                checksumEl.innerHTML = `<div style="font-size:11px;color:#ef4444;padding:6px 10px"><i class="fas fa-times"></i> ${e.message}</div>`;
            });
    }

    /* ─── Drive selected ─── */
    driveSelect.onchange = () => {
        const disk = driveSelect.value;
        if (disk) {
            const d = state.drives.find(x => x.name === disk);
            showDriveInfo(d);
            state.selectedDisk = disk;
        } else {
            driveInfo.style.display = 'none';
            state.selectedDisk = null;
        }
        updateFlashBtn();
    };

    function showDriveInfo(d) {
        if (!d) { driveInfo.style.display = 'none'; return; }
        const parts = d.partitions.map(p => `${p.name}${p.label ? ` (${p.label})` : ''} ${p.size}${p.mountpoint ? ' → ' + p.mountpoint : ''}`).join(', ');
        driveInfo.style.display = '';
        driveInfo.innerHTML = `
            <div class="fl-drive-info">
                <i class="fas fa-usb"></i>
                <div>
                    <div style="font-weight:600;color:var(--text-primary)">/dev/${d.name} — ${d.model || d.label || '?'}</div>
                    <div>Rozmiar: ${d.size}${parts ? ' | Partycje: ' + parts : ''}</div>
                </div>
            </div>
            ${d.has_mounted ? `<div class="fl-drive-warn"><i class="fas fa-exclamation-triangle"></i> ${t('Partycje zamontowane — zostaną odmontowane przed flashowaniem')}</div>` : ''}
            <div class="fl-drive-warn"><i class="fas fa-radiation"></i> ${t('UWAGA: Wszystkie dane na tym dysku zostaną usunięte!')}</div>`;
    }

    /* ─── Refresh buttons ─── */
    $('#fl-image-refresh').onclick = loadImages;
    $('#fl-drive-refresh').onclick = loadDrives;

    /* ─── Format drive ─── */
    $('#fl-drive-format').onclick = async () => {
        if (!state.selectedDisk) { toast('Wybierz dysk', 'warning'); return; }
        const fs = prompt(t('System plików (fat32, exfat, ext4, ntfs):'), 'exfat');
        if (!fs) return;
        const label = prompt(t('Etykieta dysku:'), 'USB') || 'USB';
        if (!await confirmDialog(t('Sformatować') + ` /dev/${state.selectedDisk} ` + t('jako') + ` ${fs.toUpperCase()}?\n` + t('Wszystkie dane zostaną usunięte!'))) return;
        try {
            const r = await api('/flasher/format', { method: 'POST', body: { disk: state.selectedDisk, fs_type: fs, label } });
            toast(r.message || t('Sformatowano'), 'success');
            loadDrives();
        } catch(e) { toast(t('Błąd: ') + e.message, 'error'); }
    };

    /* ─── Cancel flash ─── */
    cancelBtn.onclick = async () => {
        if (!await confirmDialog(t('Przerwać flashowanie? Dysk USB może być uszkodzony.'))) return;
        try {
            await api('/flasher/cancel', { method: 'POST' });
            toast(t('Anulowano flashowanie'), 'warning');
        } catch(e) { toast(t('Błąd: ') + e.message, 'error'); }
    };

    /* ─── FLASH ─── */
    flashBtn.onclick = async () => {
        if (!state.selectedImage || !state.selectedDisk || state.flashing) return;

        const drive = state.drives.find(x => x.name === state.selectedDisk);
        const dLabel = drive ? (drive.model || drive.label || drive.name) : state.selectedDisk;

        if (!await confirmDialog(t('UWAGA!') + `\n\n` + t('Wszystkie dane na dysku') + ` /dev/${state.selectedDisk} (${dLabel}) ` + t('zostaną bezpowrotnie usunięte.') + `\n\n` + t('Czy na pewno chcesz kontynuować?'))) return;

        state.flashing = true;
        updateFlashBtn();
        cancelBtn.style.display = '';
        stopDriveRefresh();

        // Reset UI
        logEl.innerHTML = '';
        logSection.classList.add('active');
        resultEl.style.display = 'none';
        progressW.classList.add('active');
        progressBar.style.width = '0%';
        progressTxt.textContent = '0%';
        progressMsg.textContent = 'Rozpoczynanie...';
        progressWritten.textContent = '—';
        progressSpeed.textContent = '—';
        progressEta.textContent = '—';
        progressElapsed.textContent = '—';

        // Disable controls
        imgSelect.disabled = true;
        driveSelect.disabled = true;
        imgPath.disabled = true;
        $$('.fl-btn-sm').forEach(b => b.disabled = true);

        try {
            const resp = await api('/flasher/flash', {
                method: 'POST',
                body: { image: state.selectedImage, disk: state.selectedDisk, verify: verifyCheck.checked },
            });

            if (resp.error) {
                showResult(false, resp.error);
                return;
            }

            // Started OK — poll for progress
            _logOffset = 0;
            startPolling();
        } catch (e) {
            showResult(false, t('Błąd rozpoczęcia: ') + e.message);
        }
    };

    function showResult(success, msg) {
        state.flashing = false;
        cancelBtn.style.display = 'none';
        startDriveRefresh();

        // Update progress bar title on completion
        const titleEl = progressW.querySelector('.fl-section-title');
        if (titleEl) {
            titleEl.innerHTML = success
                ? `<i class="fas fa-check-circle" style="color:#10b981"></i> ${t('Zakończono')}`
                : `<i class="fas fa-times-circle" style="color:#ef4444"></i> ${t('Błąd')}`;
        }

        resultEl.style.display = '';
        if (success) {
            resultEl.innerHTML = `
                <div class="fl-section" style="text-align:center;border:1px solid #10b981">
                    <div class="fl-result">
                        <div class="fl-result-icon" style="color:#10b981"><i class="fas fa-check-circle"></i></div>
                        <div class="fl-result-msg" style="color:#10b981">${msg}</div>
                        <div class="fl-result-detail">${t('Możesz teraz bezpiecznie wyjąć dysk USB.')}</div>
                    </div>
                </div>`;
            toast(t('Flashowanie zakończone!'), 'success');
        } else {
            resultEl.innerHTML = `
                <div class="fl-section" style="text-align:center;border:1px solid #ef4444">
                    <div class="fl-result">
                        <div class="fl-result-icon" style="color:#ef4444"><i class="fas fa-times-circle"></i></div>
                        <div class="fl-result-msg" style="color:#ef4444">${t('Błąd flashowania')}</div>
                        <div class="fl-result-detail">${msg}</div>
                    </div>
                </div>`;
            toast(t('Flashowanie nie powiodło się'), 'error');
        }

        // Re-enable controls
        imgSelect.disabled = false;
        driveSelect.disabled = false;
        imgPath.disabled = false;
        $$('.fl-btn-sm').forEach(b => b.disabled = false);
        updateFlashBtn();
    }

    /* ─── Polling for reconnect ─── */
    let _pollTimer = null;
    let _logOffset = 0;

    function startPolling() {
        stopPolling();
        _pollTimer = setInterval(pollFlashStatus, 2000);
    }

    function stopPolling() {
        if (_pollTimer) { clearInterval(_pollTimer); _pollTimer = null; }
    }

    async function pollFlashStatus() {
        try {
            const st = await api(`/flasher/status?since=${_logOffset}`);
            if (st.status === 'flashing') {
                progressBar.style.width = st.percent + '%';
                progressTxt.textContent = st.percent + '%';
                progressMsg.textContent = st.message || '';

                // Stats row
                if (st.bytes_written > 0) {
                    progressWritten.textContent = `${(st.bytes_written / (1024*1024)).toFixed(0)} / ${(st.total_bytes / (1024*1024)).toFixed(0)} MB`;
                }
                if (st.speed > 0) {
                    progressSpeed.textContent = `⚡ ${st.speed} MB/s`;
                }
                if (st.elapsed) {
                    const em = Math.floor(st.elapsed / 60);
                    const es = Math.floor(st.elapsed % 60);
                    progressElapsed.textContent = `⏱ ${em}:${String(es).padStart(2, '0')}`;
                }
                if (st.eta != null && st.eta > 0) {
                    const m = Math.floor(st.eta / 60);
                    const s = Math.floor(st.eta % 60);
                    progressEta.textContent = `ETA: ${m > 0 ? m + 'min ' : ''}${s}s`;
                } else {
                    progressEta.textContent = '';
                }

                for (const line of (st.logs || [])) addLog(line);
                _logOffset = st.log_total || 0;
            } else if (st.status === 'done' || st.status === 'error') {
                for (const line of (st.logs || [])) addLog(line);
                const success = st.result ? st.result.success : st.status === 'done';
                const msg = st.result ? st.result.message : st.message;
                if (success) {
                    progressBar.style.width = '100%';
                    progressTxt.textContent = '100%';
                }
                showResult(success, msg);
                stopPolling();
            } else {
                stopPolling();
            }
        } catch (e) {
            // keep polling
        }
    }

    async function checkFlashStatus() {
        try {
            const st = await api('/flasher/status');
            if (st.status === 'flashing') {
                state.flashing = true;
                updateFlashBtn();
                imgSelect.disabled = true;
                driveSelect.disabled = true;
                imgPath.disabled = true;
                $$('.fl-btn-sm').forEach(b => b.disabled = true);

                logEl.innerHTML = '';
                logSection.classList.add('active');
                resultEl.style.display = 'none';
                progressW.classList.add('active');
                cancelBtn.style.display = '';
                stopDriveRefresh();
                progressBar.style.width = st.percent + '%';
                progressTxt.textContent = st.percent + '%';
                progressMsg.textContent = st.message || 'Reconnected...';
                for (const line of (st.logs || [])) addLog(line);
                _logOffset = st.log_total || 0;
                startPolling();
            } else if (st.status === 'done' || st.status === 'error') {
                const success = st.result ? st.result.success : st.status === 'done';
                const msg = st.result ? st.result.message : st.message;
                logEl.innerHTML = '';
                logSection.classList.add('active');
                for (const line of (st.logs || [])) addLog(line);
                progressW.classList.add('active');
                progressBar.style.width = success ? '100%' : '0%';
                progressTxt.textContent = success ? '100%' : '0%';
                showResult(success, msg);
            }
        } catch (e) {
            // fresh start
        }
    }

    /* ─── Auto-refresh drives ─── */
    let _driveRefreshTimer = null;
    function startDriveRefresh() {
        stopDriveRefresh();
        _driveRefreshTimer = setInterval(() => {
            if (!state.flashing) loadDrives();
        }, 5000);
    }
    function stopDriveRefresh() {
        if (_driveRefreshTimer) { clearInterval(_driveRefreshTimer); _driveRefreshTimer = null; }
    }

    /* ─── History ─── */
    async function loadHistory() {
        try {
            const data = await api('/flasher/history');
            const countEl = $('#fl-history-count');
            const listEl = $('#fl-history-list');
            countEl.textContent = data.length ? `(${data.length})` : '';
            if (!data.length) {
                listEl.innerHTML = '<div style="font-size:12px;color:var(--text-muted);padding:8px">Brak historii</div>';
                return;
            }
            listEl.innerHTML = data.reverse().map(h => `
                <div style="display:flex;align-items:center;gap:8px;padding:6px 0;border-bottom:1px solid var(--border);font-size:12px">
                    <i class="fas fa-${h.success ? 'check-circle' : 'times-circle'}" style="color:${h.success ? '#10b981' : '#ef4444'}"></i>
                    <div style="flex:1;min-width:0">
                        <div style="white-space:nowrap;overflow:hidden;text-overflow:ellipsis;color:var(--text-primary)">${h.image.split('/').pop()}</div>
                        <div style="color:var(--text-muted);font-size:11px">/dev/${h.disk} — ${humanSize(h.size)} — ${h.elapsed ? Math.round(h.elapsed) + 's' : '?'}</div>
                    </div>
                    <div style="font-size:10px;color:var(--text-muted);white-space:nowrap">${new Date(h.timestamp * 1000).toLocaleString(getLocale())}</div>
                </div>
            `).join('');
        } catch(e) {}
    }

    $('#fl-history-toggle').onclick = () => {
        const list = $('#fl-history-list');
        const visible = list.style.display !== 'none';
        list.style.display = visible ? 'none' : '';
        if (!visible) loadHistory();
    };

    /* ─── Init ─── */
    loadImages();
    loadDrives();
    checkFlashStatus();
    startDriveRefresh();
}
