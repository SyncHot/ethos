/* ═══════════════════════════════════════════════════════════
   EthOS — Aktualizacje systemu (OTA)
   Sprawdzanie, pobieranie i instalowanie aktualizacji
   ═══════════════════════════════════════════════════════════ */

AppRegistry['updates'] = function (appDef) {
    createWindow('updates', {
        title: t('Aktualizacje systemu'),
        icon: appDef.icon,
        iconColor: appDef.color,
        width: 820,
        height: 560,
        singleton: true,
        onRender: (body) => renderUpdatesApp(body),
    });
};

function renderUpdatesApp(body) {
    const $ = (s) => body.querySelector(s);
    let config = {};
    let status = {};
    let logs = [];

    /* ────────────── HTML ────────────── */
    body.innerHTML = `
    <style>
    .upd-sidebar{width:180px;min-width:180px;background:var(--bg-secondary,#0f172a);border-right:1px solid var(--border);display:flex;flex-direction:column;padding:8px 0;flex-shrink:0}
    .upd-nav{padding:10px 18px;cursor:pointer;display:flex;align-items:center;gap:10px;font-size:13px;color:var(--text-secondary,#94a3b8);transition:.15s;border-left:3px solid transparent}
    .upd-nav:hover{background:var(--bg-hover,rgba(255,255,255,.04));color:var(--text-primary,#e2e8f0)}
    .upd-nav.active{background:var(--bg-hover,rgba(255,255,255,.06));color:#6366f1;border-left-color:#6366f1;font-weight:600}
    .upd-nav i{width:16px;text-align:center;font-size:12px}
    </style>
    <div class="upd-app" style="display:flex;height:100%;font-family:var(--font);color:var(--text);background:var(--bg-primary);">
        <div class="upd-sidebar">
            <div class="upd-nav active" data-tab="update"><i class="fas fa-download"></i> Aktualizacja</div>
            <div class="upd-nav" data-tab="settings"><i class="fas fa-cog"></i> Ustawienia</div>
            <div class="upd-nav" data-tab="log"><i class="fas fa-terminal"></i> Log</div>
        </div>
        <div style="flex:1;display:flex;flex-direction:column;overflow:hidden;">

        <!-- HEADER — current version + status -->
        <div class="upd-header" style="padding:20px 24px 16px;border-bottom:1px solid var(--border);">
            <div style="display:flex;align-items:center;gap:14px;">
                <div style="width:48px;height:48px;border-radius:14px;background:linear-gradient(135deg,#6366f1,#8b5cf6);display:flex;align-items:center;justify-content:center;">
                    <i class="fas fa-cloud-download-alt" style="font-size:22px;color:#fff"></i>
                </div>
                <div style="flex:1">
                    <div style="font-size:18px;font-weight:700;" id="upd-title">EthOS</div>
                    <div style="font-size:13px;color:var(--text-secondary);" id="upd-ver-line">Wersja: <span id="upd-cur-ver">…</span></div>
                </div>
                <button class="btn btn-primary" id="upd-check-btn" style="gap:6px;font-size:13px;">
                    <i class="fas fa-sync-alt"></i> ${t('Sprawdź aktualizacje')}
                </button>
            </div>
            <div id="upd-status-bar" style="margin-top:12px;display:none;"></div>
        </div>

        <div style="flex:1;overflow-y:auto;">
            <!-- TAB: UPDATE -->
            <div class="upd-pane" id="upd-pane-update" style="padding:24px;">
                <div id="upd-no-update" style="text-align:center;padding:40px 0;color:var(--text-secondary);">
                    <i class="fas fa-check-circle" style="font-size:48px;color:#22c55e;margin-bottom:16px;display:block;"></i>
                    <div style="font-size:15px;font-weight:600;">System jest aktualny</div>
                    <div style="font-size:13px;margin-top:6px;" id="upd-last-check-msg"></div>
                </div>
                <div id="upd-available" style="display:none;">
                    <div style="background:var(--bg-secondary);border:1px solid var(--border);border-radius:12px;padding:20px;">
                        <div style="display:flex;align-items:center;gap:12px;margin-bottom:14px;">
                            <div style="width:36px;height:36px;border-radius:10px;background:linear-gradient(135deg,#22c55e,#16a34a);display:flex;align-items:center;justify-content:center;">
                                <i class="fas fa-gift" style="font-size:16px;color:#fff"></i>
                            </div>
                            <div style="flex:1;">
                                <div style="font-size:15px;font-weight:700;">${t('Dostępna wersja')} <span id="upd-remote-ver"></span></div>
                                <div style="font-size:12px;color:var(--text-secondary);" id="upd-pkg-size"></div>
                            </div>
                        </div>
                        <div id="upd-changelog" style="font-size:13px;line-height:1.6;margin-bottom:16px;max-height:180px;overflow-y:auto;"></div>
                        <div style="display:flex;gap:10px;">
                            <button class="btn btn-primary" id="upd-apply-btn" style="gap:6px;">
                                <i class="fas fa-rocket"></i> ${t('Zainstaluj aktualizację')}
                            </button>
                        </div>
                    </div>
                </div>
                <div id="upd-progress-area" style="display:none;margin-top:16px;">
                    <div style="background:var(--bg-secondary);border:1px solid var(--border);border-radius:12px;padding:18px 20px;">
                        <div style="display:flex;align-items:center;gap:10px;margin-bottom:12px;">
                            <i class="fas fa-spinner fa-spin" style="font-size:16px;color:var(--accent);"></i>
                            <div style="flex:1;">
                                <div style="font-size:14px;font-weight:700;color:var(--text);" id="upd-progress-label">${t('Aktualizuję…')}</div>
                                <div style="font-size:12px;color:var(--text-secondary);margin-top:2px;" id="upd-progress-step"></div>
                            </div>
                            <div style="font-size:20px;font-weight:700;color:var(--accent);min-width:48px;text-align:right;" id="upd-progress-pct">0%</div>
                        </div>
                        <div style="background:var(--bg-primary);border-radius:6px;height:8px;overflow:hidden;">
                            <div id="upd-progress-fill" style="height:100%;background:linear-gradient(90deg,#6366f1,#8b5cf6);width:0%;transition:width .4s ease;border-radius:6px;"></div>
                        </div>
                        <div id="upd-progress-steps" style="display:flex;justify-content:space-between;margin-top:10px;font-size:10px;color:var(--text-secondary);">
                            <span data-at="0" class="upd-step-dot">● Pobieranie</span>
                            <span data-at="55" class="upd-step-dot">● Rozpakowywanie</span>
                            <span data-at="70" class="upd-step-dot">● Backup</span>
                            <span data-at="80" class="upd-step-dot">● Instalacja</span>
                            <span data-at="95" class="upd-step-dot">● Restart</span>
                        </div>
                    </div>
                </div>

                <!-- Manual upload -->
                <div style="margin-top:24px;border-top:1px solid var(--border);padding-top:18px;">
                    <div style="font-size:13px;font-weight:600;margin-bottom:8px;">
                        <i class="fas fa-file-upload" style="margin-right:6px;"></i> ${t('Ręczna aktualizacja z pliku')}
                    </div>
                    <div style="display:flex;gap:10px;align-items:center;">
                        <input type="file" id="upd-file-input" accept=".tar.gz" style="font-size:12px;flex:1;">
                        <button class="btn btn-secondary" id="upd-upload-btn" style="font-size:12px;gap:4px;">
                            <i class="fas fa-upload"></i> ${t('Wyślij')}
                        </button>
                    </div>
                </div>
            </div>

            <!-- TAB: SETTINGS -->
            <div class="upd-pane" id="upd-pane-settings" style="padding:24px;display:none;">
                <div style="display:flex;flex-direction:column;gap:18px;">

                    <!-- Publish — make this instance an update server -->
                    <div style="background:var(--bg-secondary);border:1px solid var(--border);border-radius:12px;padding:16px 18px;">
                        <div style="display:flex;align-items:center;gap:10px;margin-bottom:10px;">
                            <i class="fas fa-broadcast-tower" style="font-size:16px;color:var(--accent);"></i>
                            <span style="font-size:14px;font-weight:700;">Serwer aktualizacji</span>
                        </div>
                        <div style="font-size:12px;color:var(--text-secondary);margin-bottom:12px;">
                            ${t('Opublikuj obecną wersję, aby inne instancje EthOS mogły się do niej zaktualizować.')}
                            ${t('Po publikacji wystarczy podać IP tego urządzenia jako źródło aktualizacji.')}
                        </div>
                        <div style="display:flex;gap:10px;align-items:center;">
                            <button class="btn btn-secondary" id="upd-publish-btn" style="gap:6px;font-size:13px;">
                                <i class="fas fa-cloud-upload-alt"></i> ${t('Opublikuj obecną wersję')}
                            </button>
                            <span id="upd-publish-status" style="font-size:12px;color:var(--text-secondary);"></span>
                        </div>
                    </div>

                    <!-- Update source URL -->
                    <div>
                        <label style="font-size:13px;font-weight:600;display:block;margin-bottom:6px;">${t('Źródło aktualizacji')}</label>
                        <input type="text" id="upd-url" placeholder="192.168.1.100 lub ethos.local lub github:user/repo" style="width:100%;padding:9px 12px;border-radius:8px;border:1px solid var(--border);background:var(--bg-secondary);color:var(--text);font-size:13px;box-sizing:border-box;">
                        <div style="font-size:11px;color:var(--text-secondary);margin-top:4px;line-height:1.5;">
                            ${t('Podaj adres źródła aktualizacji — port nie jest wymagany:')}<br>
                            <b>${t('IP lub hostname')}</b> — ${t('np.')} <code>192.168.1.100</code> ${t('lub')} <code>ethos.local</code> (${t('łączy z portem :9000 automatycznie')})<br>
                            <b>GitHub</b> — <code>github:user/repo</code> (pobiera z GitHub Releases)<br>
                            <b>${t('Pełny URL')}</b> — ${t('np.')} <code>https://nas.example.com</code> ${t('lub')} <code>https://nas.example.com/updates</code>
                        </div>
                    </div>
                    <div style="display:flex;gap:20px;">
                        <label style="font-size:13px;display:flex;align-items:center;gap:8px;cursor:pointer;">
                            <input type="checkbox" id="upd-auto-check"> Automatycznie sprawdzaj
                        </label>
                        <label style="font-size:13px;display:flex;align-items:center;gap:8px;cursor:pointer;">
                            <input type="checkbox" id="upd-auto-apply"> Automatycznie instaluj
                        </label>
                    </div>
                    <div>
                        <label style="font-size:13px;font-weight:600;display:block;margin-bottom:6px;">${t('Interwał sprawdzania (godziny)')}</label>
                        <input type="number" id="upd-interval" min="1" max="720" value="24" style="width:120px;padding:9px 12px;border-radius:8px;border:1px solid var(--border);background:var(--bg-secondary);color:var(--text);font-size:13px;">
                    </div>
                    <div>
                        <button class="btn btn-primary" id="upd-save-cfg" style="gap:6px;font-size:13px;">
                            <i class="fas fa-save"></i> Zapisz ustawienia
                        </button>
                    </div>
                </div>
            </div>

            <!-- TAB: LOG -->
            <div class="upd-pane" id="upd-pane-log" style="padding:16px;display:none;">
                <div id="upd-log" style="font-family:'JetBrains Mono',monospace;font-size:12px;line-height:1.8;background:var(--bg-secondary);border:1px solid var(--border);border-radius:8px;padding:14px;min-height:200px;max-height:380px;overflow-y:auto;white-space:pre-wrap;color:var(--text-secondary);"></div>
            </div>
        </div>
        </div>
    </div>`;

    /* ────────────── Tabs ────────────── */
    body.querySelectorAll('.upd-nav').forEach(tab => {
        tab.onclick = () => {
            body.querySelectorAll('.upd-nav').forEach(t => t.classList.remove('active'));
            tab.classList.add('active');
            body.querySelectorAll('.upd-pane').forEach(p => p.style.display = 'none');
            const pane = $(`#upd-pane-${tab.dataset.tab}`);
            if (pane) pane.style.display = '';
        };
    });

    /* ────────────── Helpers ────────────── */
    function fmtBytes(b) {
        if (!b) return '';
        if (b < 1048576) return (b / 1024).toFixed(0) + ' KB';
        return (b / 1048576).toFixed(1) + ' MB';
    }
    function fmtDate(iso) {
        if (!iso) return '–';
        try {
            const d = new Date(iso);
            return d.toLocaleString(getLocale(), { day: '2-digit', month: '2-digit', year: 'numeric', hour: '2-digit', minute: '2-digit' });
        } catch { return iso; }
    }
    function addLog(msg, isError) {
        const ts = new Date().toLocaleTimeString(getLocale(), { hour: '2-digit', minute: '2-digit', second: '2-digit' });
        logs.push({ ts, msg, isError });
        const el = $('#upd-log');
        if (el) {
            const line = document.createElement('div');
            line.style.color = isError ? '#ef4444' : 'var(--text-secondary)';
            line.textContent = `[${ts}] ${msg}`;
            el.appendChild(line);
            el.scrollTop = el.scrollHeight;
        }
    }

    /* ────────────── UI update ────────────── */
    function refreshUI() {
        const curVer = $('#upd-cur-ver');
        if (curVer) curVer.textContent = status.current_version || config.current_version || '…';

        // Last check
        const lastMsg = $('#upd-last-check-msg');
        if (lastMsg) {
            const last = status.last_check || config.last_check;
            lastMsg.textContent = last ? `${t('Ostatnie sprawdzenie')}: ${fmtDate(last)}` : t('Nie sprawdzano jeszcze');
        }

        // Available update
        const available = status.available;
        const noUp = $('#upd-no-update');
        const avUp = $('#upd-available');

        if (available) {
            noUp.style.display = 'none';
            avUp.style.display = '';
            $('#upd-remote-ver').textContent = available.version || '?';
            const sizeEl = $('#upd-pkg-size');
            if (sizeEl) sizeEl.textContent = available.size ? `${t('Rozmiar')}: ${fmtBytes(available.size)}` : '';
            renderChangelog(available.changelog);
        } else {
            noUp.style.display = '';
            avUp.style.display = 'none';
        }

        // Progress
        const progArea = $('#upd-progress-area');
        const busy = status.downloading || status.applying;
        if (busy) {
            progArea.style.display = '';
            const pct = status.progress || 0;
            $('#upd-progress-fill').style.width = pct + '%';
            $('#upd-progress-pct').textContent = pct + '%';
            const label = $('#upd-progress-label');
            if (status.downloading) label.textContent = t('Pobieranie aktualizacji…');
            else if (status.applying) label.textContent = t('Instalowanie aktualizacji…');
            const stepEl = $('#upd-progress-step');
            if (stepEl) stepEl.textContent = status.message || '';
            // Highlight completed steps
            body.querySelectorAll('.upd-step-dot').forEach(dot => {
                const at = parseInt(dot.dataset.at);
                if (pct >= at) {
                    dot.style.color = 'var(--accent)';
                    dot.style.fontWeight = '600';
                } else {
                    dot.style.color = 'var(--text-secondary)';
                    dot.style.fontWeight = '400';
                }
            });
        } else {
            progArea.style.display = 'none';
        }

        // Button states
        const checkBtn = $('#upd-check-btn');
        const applyBtn = $('#upd-apply-btn');
        if (checkBtn) {
            checkBtn.disabled = status.checking || busy;
            if (status.checking) {
                checkBtn.innerHTML = '<i class="fas fa-spinner fa-spin"></i> ' + t('Sprawdzanie…');
            } else {
                checkBtn.innerHTML = `<i class="fas fa-sync-alt"></i> ${t('Sprawdź aktualizacje')}`;
            }
        }
        if (applyBtn) {
            applyBtn.disabled = busy;
        }

        // Status bar
        const bar = $('#upd-status-bar');
        if (status.error) {
            bar.style.display = '';
            bar.innerHTML = `<div style="background:rgba(239,68,68,.1);border:1px solid rgba(239,68,68,.2);border-radius:8px;padding:10px 14px;font-size:12px;color:#ef4444;">
                <i class="fas fa-exclamation-triangle" style="margin-right:6px;"></i>${status.error}
            </div>`;
        } else if (busy) {
            bar.style.display = '';
            const msg = status.message || (status.downloading ? t('Pobieranie aktualizacji…') : t('Instalowanie aktualizacji…'));
            bar.innerHTML = `<div style="background:rgba(99,102,241,.08);border:1px solid rgba(99,102,241,.15);border-radius:8px;padding:10px 14px;font-size:12px;color:var(--accent);">
                <i class="fas fa-spinner fa-spin" style="margin-right:6px;"></i>${msg}
            </div>`;
        } else {
            bar.style.display = 'none';
        }
    }

    function renderChangelog(cl) {
        const box = $('#upd-changelog');
        if (!box) return;
        if (!cl || typeof cl !== 'object') {
            box.innerHTML = '';
            return;
        }
        // cl can be { title: "...", changes: [...] } or just text
        let html = '';
        if (cl.title) html += `<div style="font-weight:600;margin-bottom:6px;">${cl.title}</div>`;
        if (Array.isArray(cl.changes)) {
            html += '<ul style="margin:0;padding-left:20px;">';
            cl.changes.forEach(c => { html += `<li>${c}</li>`; });
            html += '</ul>';
        } else if (typeof cl === 'string') {
            html = `<p>${cl}</p>`;
        }
        box.innerHTML = html;
    }

    /* ────────────── Settings ────────────── */
    function fillSettings() {
        const url = $('#upd-url');
        const autoCheck = $('#upd-auto-check');
        const autoApply = $('#upd-auto-apply');
        const intv = $('#upd-interval');
        if (url) url.value = config.update_url || '';
        if (autoCheck) autoCheck.checked = config.auto_check !== false;
        if (autoApply) autoApply.checked = !!config.auto_apply;
        if (intv) intv.value = Math.round((config.auto_check_interval || 86400) / 3600);
    }

    /* ────────────── API ────────────── */
    async function loadConfig() {
        try {
            const data = await api('/update/config');
            config = data;
            fillSettings();
        } catch (e) { addLog(t('Błąd ładowania konfiguracji: ') + e, true); }
    }

    async function loadStatus() {
        try {
            const data = await api('/update/status');
            status = data;
            refreshUI();
        } catch (e) { addLog(t('Błąd statusu: ') + e, true); }
    }

    async function checkUpdates() {
        status.checking = true;
        status.error = null;
        refreshUI();
        addLog('Sprawdzanie aktualizacji…');
        try {
            const data = await api('/update/check', { method: 'POST' });
            if (data.error) {
                status.checking = false;
                status.error = data.error;
                addLog(status.error, true);
            } else if (data.update_available) {
                status.checking = false;
                status.available = data.manifest;
                addLog(t('Dostępna wersja') + ` ${data.remote_version} (` + t('obecna:') + ` ${data.current_version})`);
            } else {
                status.checking = false;
                status.available = null;
                addLog(data.message || 'System aktualny');
            }
        } catch (e) {
            status.checking = false;
            status.error = t('Błąd połączenia: ') + e;
            addLog(status.error, true);
        }
        refreshUI();
    }

    async function applyUpdate() {
        if (!confirm(t('Zainstalować aktualizację? System zostanie zrestartowany.'))) return;
        addLog(t('Rozpoczynam instalację aktualizacji…'));
        try {
            const data = await api('/update/apply', { method: 'POST' });
            if (data.error) {
                toast(data.error, 'error');
                addLog(data.error, true);
            }
        } catch (e) {
            toast(t('Błąd: ') + e, 'error');
            addLog(t('Błąd: ') + e, true);
        }
    }

    async function uploadPackage() {
        const input = $('#upd-file-input');
        if (!input || !input.files.length) {
            toast('Wybierz plik .tar.gz', 'warning');
            return;
        }
        const file = input.files[0];
        if (!file.name.endsWith('.tar.gz')) {
            toast('Wymagany plik .tar.gz', 'warning');
            return;
        }
        if (!confirm(t('Zainstalować z pliku') + ` ${file.name}? ` + t('System zostanie zrestartowany.'))) return;

        addLog(t('Przesyłanie pliku') + ` ${file.name} (${fmtBytes(file.size)})…`);
        const fd = new FormData();
        fd.append('file', file);

        try {
            const res = await fetch('/api/update/upload', {
                method: 'POST',
                headers: { 'Authorization': `Bearer ${NAS.token}`, 'X-CSRFToken': NAS.csrfToken },
                body: fd
            });
            const data = await res.json();
            if (!res.ok) {
                toast(data.error || t('Błąd'), 'error');
                addLog(data.error, true);
            } else {
                toast(t('Aktualizacja z pliku rozpoczęta'), 'info');
            }
        } catch (e) {
            toast(t('Błąd: ') + e, 'error');
            addLog(t('Błąd przesyłania: ') + e, true);
        }
    }

    async function saveConfig() {
        const payload = {
            update_url: ($('#upd-url') || {}).value || '',
            auto_check: ($('#upd-auto-check') || {}).checked || false,
            auto_apply: ($('#upd-auto-apply') || {}).checked || false,
            auto_check_interval: Math.max(1, parseInt($('#upd-interval')?.value || '24')) * 3600,
        };
        try {
            const data = await api('/update/config', {
                method: 'PUT',
                headers: { 'Content-Type': 'application/json' },
                body: payload,
            });
            if (data.success) {
                config = data.config || config;
                toast('Ustawienia zapisane', 'success');
                addLog('Ustawienia zapisane');
            } else {
                toast(data.error || t('Błąd zapisu'), 'error');
            }
        } catch (e) {
            toast(t('Błąd: ') + e, 'error');
        }
    }

    async function publishUpdate() {
        const btn = $('#upd-publish-btn');
        const statusEl = $('#upd-publish-status');
        if (!confirm(t('Opublikować obecną wersję jako aktualizację?'))) return;
        btn.disabled = true;
        btn.innerHTML = `<i class="fas fa-spinner fa-spin"></i> ${t('Publikuję…')}`;
        statusEl.textContent = '';
        addLog('Publikowanie aktualizacji…');
        try {
            const data = await api('/update/publish', { method: 'POST' });
            if (data.success) {
                const m = data.manifest;
                const msg = `Opublikowano v${m.version} (${fmtBytes(m.size)})`;
                statusEl.textContent = msg;
                statusEl.style.color = '#22c55e';
                toast(msg, 'success');
                addLog(msg);
            } else {
                const err = data.error || t('Błąd publikacji');
                statusEl.textContent = err;
                statusEl.style.color = '#ef4444';
                toast(err, 'error');
                addLog(err, true);
            }
        } catch (e) {
            statusEl.textContent = t('Błąd: ') + e;
            statusEl.style.color = '#ef4444';
            toast(t('Błąd: ') + e, 'error');
            addLog(t('Błąd publikacji: ') + e, true);
        }
        btn.disabled = false;
        btn.innerHTML = `<i class="fas fa-cloud-upload-alt"></i> ${t('Opublikuj obecną wersję')}`;
    }

    /* ────────────── SocketIO events ────────────── */
    function setupSocket() {
        if (typeof socket === 'undefined') return;

        socket.on('update_status', (data) => {
            Object.assign(status, data);
            refreshUI();
        });

        socket.on('update_log', (data) => {
            addLog(data.message, data.error);
        });

        socket.on('update_available', (data) => {
            addLog(t('Dostępna aktualizacja:') + ` ${data.remote} (` + t('obecna:') + ` ${data.current})`);
            toast(`${t('Dostępna aktualizacja EthOS')} ${data.remote}`, 'info');
            loadStatus();
        });

        socket.on('update_complete', (data) => {
            addLog(t('Aktualizacja do') + ` ${data.version} ` + t('zakończona!'));
            toast(`EthOS zaktualizowany do wersji ${data.version}${t('! Odśwież stronę.')}`, 'success');
            status.applying = false;
            status.downloading = false;
            status.available = null;
            status.progress = 100;
            refreshUI();

            // Prompt reload after a brief delay (container restarts)
            setTimeout(() => {
                if (confirm(t('Aktualizacja do') + ` ${data.version} ` + t('zakończona.') + '\n' + t('Odświeżyć stronę?'))) {
                    location.reload();
                }
            }, 3000);
        });
    }

    /* ────────────── Event handlers ────────────── */
    $('#upd-check-btn').onclick = checkUpdates;
    $('#upd-apply-btn').onclick = applyUpdate;
    $('#upd-upload-btn').onclick = uploadPackage;
    $('#upd-save-cfg').onclick = saveConfig;
    $('#upd-publish-btn').onclick = publishUpdate;

    /* ────────────── Init ────────────── */
    setupSocket();
    loadConfig().then(() => loadStatus());
}
