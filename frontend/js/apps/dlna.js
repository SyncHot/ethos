/* ═══════════════════════════════════════════════════════════
   EthOS — DLNA / UPnP Media Server
   Install, configure and manage MiniDLNA for media streaming
   ═══════════════════════════════════════════════════════════ */

AppRegistry['dlna'] = function (appDef) {
    createWindow('dlna', {
        title: 'DLNA / UPnP',
        icon: appDef.icon,
        iconColor: appDef.color,
        width: 720,
        height: 620,
        onRender: (body) => renderDLNAApp(body),
    });
};

function renderDLNAApp(body) {
    body.innerHTML = `
    <div class="dlna-app">
        <!-- Status card -->
        <div class="dlna-status-card" id="dlna-status-card">
            <div class="dlna-status-row">
                <div class="dlna-status-indicator">
                    <i class="fas fa-circle" id="dlna-status-dot"></i>
                    <span id="dlna-status-text">Sprawdzanie…</span>
                </div>
                <div class="dlna-status-actions" id="dlna-status-actions"></div>
            </div>
            <div class="dlna-status-stats" id="dlna-stats" style="display:none">
                <div class="dlna-stat"><i class="fas fa-film"></i> <span id="dlna-file-count">0</span> ${t('plików')}</div>
                <div class="dlna-stat"><i class="fas fa-network-wired"></i> Port: <span id="dlna-port">8200</span></div>
                <div class="dlna-stat"><i class="fas fa-signature"></i> <span id="dlna-name-display">—</span></div>
            </div>
        </div>

        <!-- Install panel (hidden when installed) -->
        <div class="dlna-install-panel" id="dlna-install-panel" style="display:none">
            <i class="fas fa-download" style="font-size:32px;color:var(--accent);margin-bottom:10px"></i>
            <p>MiniDLNA nie jest zainstalowany.</p>
            <button class="dlna-btn dlna-btn-primary" id="dlna-install-btn">
                <i class="fas fa-download"></i> Zainstaluj MiniDLNA
            </button>
        </div>

        <!-- Config form (hidden when not installed) -->
        <div class="dlna-config-panel" id="dlna-config-panel" style="display:none">
            <div class="dlna-section-header">
                <h3><i class="fas fa-cog"></i> Konfiguracja</h3>
            </div>

            <div class="dlna-form-row">
                <label>Nazwa serwera</label>
                <input type="text" id="dlna-friendly-name" class="fm-input" maxlength="64"
                       placeholder="EthOS Media Server">
            </div>

            <div class="dlna-form-row">
                <label>Port</label>
                <input type="number" id="dlna-port-input" class="fm-input" style="width:100px"
                       min="1024" max="65535" value="8200">
            </div>

            <div class="dlna-form-row dlna-form-row-top">
                <label>${t('Katalogi mediów')}</label>
                <div class="dlna-media-dirs" id="dlna-media-dirs">
                    <p class="dlna-muted">${t('Ładowanie dysków…')}</p>
                </div>
            </div>

            <div class="dlna-form-row">
                <label>Automatyczne wykrywanie</label>
                <label class="dlna-toggle">
                    <input type="checkbox" id="dlna-inotify" checked>
                    <span class="dlna-toggle-slider"></span>
                    <span class="dlna-toggle-label">inotify — wykrywaj nowe pliki automatycznie</span>
                </label>
            </div>

            <div class="dlna-form-actions">
                <button class="dlna-btn dlna-btn-primary" id="dlna-save-btn">
                    <i class="fas fa-save"></i> ${t('Zapisz konfigurację')}
                </button>
                <button class="dlna-btn dlna-btn-secondary" id="dlna-rescan-btn">
                    <i class="fas fa-sync-alt"></i> ${t('Pełne skanowanie')}
                </button>
            </div>
        </div>
    </div>`;

    const $ = (sel) => body.querySelector(sel);
    const $$ = (sel) => body.querySelectorAll(sel);

    let currentConfig = {};
    let availableDrives = [];

    /* ── Status ── */
    async function loadStatus() {
        try {
            const data = await api('/dlna/status');

            if (!data.installed) {
                $('#dlna-status-dot').style.color = 'var(--text-muted)';
                $('#dlna-status-text').textContent = 'Nie zainstalowano';
                $('#dlna-install-panel').style.display = 'flex';
                $('#dlna-config-panel').style.display = 'none';
                $('#dlna-stats').style.display = 'none';
                $('#dlna-status-actions').innerHTML = '';
                return;
            }

            $('#dlna-install-panel').style.display = 'none';
            $('#dlna-config-panel').style.display = 'block';

            if (data.running) {
                $('#dlna-status-dot').style.color = '#2dd4a8';
                $('#dlna-status-text').textContent = 'Uruchomiony';
                $('#dlna-stats').style.display = 'flex';
                $('#dlna-file-count').textContent = data.file_count || 0;
                $('#dlna-port').textContent = data.port || 8200;
                $('#dlna-name-display').textContent = data.friendly_name || '—';
                $('#dlna-status-actions').innerHTML = `
                    <button class="dlna-btn dlna-btn-danger dlna-btn-sm" id="dlna-stop-btn">
                        <i class="fas fa-stop"></i> Zatrzymaj
                    </button>`;
                $('#dlna-stop-btn').onclick = stopService;
            } else {
                $('#dlna-status-dot').style.color = '#ff4d6a';
                $('#dlna-status-text').textContent = 'Zatrzymany';
                $('#dlna-stats').style.display = 'none';
                $('#dlna-status-actions').innerHTML = `
                    <button class="dlna-btn dlna-btn-primary dlna-btn-sm" id="dlna-start-btn">
                        <i class="fas fa-play"></i> Uruchom
                    </button>`;
                $('#dlna-start-btn').onclick = startService;
            }
        } catch (e) {
            $('#dlna-status-dot').style.color = 'var(--text-muted)';
            $('#dlna-status-text').textContent = t('Błąd połączenia');
        }
    }

    /* ── Config ── */
    async function loadConfig() {
        try {
            const data = await api('/dlna/config');
            currentConfig = data;
            availableDrives = data.available_drives || [];

            $('#dlna-friendly-name').value = data.friendly_name || 'EthOS Media Server';
            $('#dlna-port-input').value = data.port || 8200;
            $('#dlna-inotify').checked = data.inotify !== false;

            renderMediaDirs(data.media_dirs || []);
        } catch (e) {
            toast('Nie udało się załadować konfiguracji DLNA', 'error');
        }
    }

    function renderMediaDirs(selectedDirs) {
        const container = $('#dlna-media-dirs');
        if (!availableDrives.length) {
            container.innerHTML = '<p class="dlna-muted">' + t('Brak zamontowanych dysków') + '</p>';
            return;
        }

        // Parse selected dirs to extract paths and type prefixes
        const selectedMap = {};
        selectedDirs.forEach(d => {
            const match = d.match(/^([AVP]),(.+)$/);
            if (match) {
                selectedMap[match[2]] = match[1];
            } else {
                selectedMap[d] = '';
            }
        });

        let html = '';
        availableDrives.forEach(drive => {
            const isSelected = drive in selectedMap;
            const types = selectedMap[drive] || 'AVP';
            html += `
            <div class="dlna-drive-row">
                <label class="dlna-drive-check">
                    <input type="checkbox" data-drive="${_esc(drive)}" ${isSelected ? 'checked' : ''}>
                    <span class="dlna-drive-path">${_esc(drive)}</span>
                </label>
                <div class="dlna-media-types" data-drive-types="${_esc(drive)}">
                    <button class="dlna-type-tag ${types.includes('V') ? 'active' : ''}" data-type="V" title="Video">V</button>
                    <button class="dlna-type-tag ${types.includes('A') ? 'active' : ''}" data-type="A" title="Audio">A</button>
                    <button class="dlna-type-tag ${types.includes('P') ? 'active' : ''}" data-type="P" title="Pictures">P</button>
                </div>
            </div>`;
        });
        container.innerHTML = html;

        // Type tag toggles
        container.querySelectorAll('.dlna-type-tag').forEach(btn => {
            btn.onclick = () => btn.classList.toggle('active');
        });
    }

    function _esc(s) {
        const d = document.createElement('div');
        d.textContent = s;
        return d.innerHTML;
    }

    function collectMediaDirs() {
        const dirs = [];
        body.querySelectorAll('#dlna-media-dirs input[type="checkbox"]').forEach(cb => {
            if (!cb.checked) return;
            const drive = cb.dataset.drive;
            const typesContainer = body.querySelector(`[data-drive-types="${CSS.escape(drive)}"]`);
            let types = '';
            if (typesContainer) {
                typesContainer.querySelectorAll('.dlna-type-tag.active').forEach(t => {
                    types += t.dataset.type;
                });
            }
            if (types && types !== 'AVP') {
                dirs.push(`${types},${drive}`);
            } else {
                dirs.push(drive);
            }
        });
        return dirs;
    }

    /* ── Actions ── */
    async function installDLNA() {
        const btn = $('#dlna-install-btn');
        btn.disabled = true;
        btn.innerHTML = '<i class="fas fa-spinner fa-spin"></i> Instalowanie…';
        try {
            const data = await api('/dlna/install', { method: 'POST' });
            if (data.success) {
                toast('MiniDLNA zainstalowano pomyślnie', 'success');
                loadStatus();
                loadConfig();
            } else {
                toast(data.error || t('Instalacja nie powiodła się'), 'error');
            }
        } catch (e) {
            toast(`${t('Błąd:')} ${e.message}`, 'error');
        } finally {
            btn.disabled = false;
            btn.innerHTML = '<i class="fas fa-download"></i> Zainstaluj MiniDLNA';
        }
    }

    async function startService() {
        try {
            const data = await api('/dlna/start', { method: 'POST' });
            if (data.success) {
                toast('DLNA uruchomiony', 'success');
            } else {
                toast(data.error || t('Nie udało się uruchomić'), 'error');
            }
        } catch (e) {
            toast(`${t('Błąd:')} ${e.message}`, 'error');
        }
        loadStatus();
    }

    async function stopService() {
        try {
            const data = await api('/dlna/stop', { method: 'POST' });
            if (data.success) {
                toast('DLNA zatrzymany', 'success');
            } else {
                toast(data.error || t('Nie udało się zatrzymać'), 'error');
            }
        } catch (e) {
            toast(`${t('Błąd:')} ${e.message}`, 'error');
        }
        loadStatus();
    }

    async function saveConfig() {
        const btn = $('#dlna-save-btn');
        btn.disabled = true;
        try {
            const payload = {
                friendly_name: $('#dlna-friendly-name').value.trim(),
                port: parseInt($('#dlna-port-input').value, 10),
                media_dirs: collectMediaDirs(),
                inotify: $('#dlna-inotify').checked,
            };
            const data = await api('/dlna/config', {
                method: 'PUT',
                body: payload,
            });
            if (data.success) {
                toast('Konfiguracja zapisana', 'success');
                loadStatus();
            } else {
                toast(data.error || t('Nie udało się zapisać'), 'error');
            }
        } catch (e) {
            toast(`${t('Błąd:')} ${e.message}`, 'error');
        } finally {
            btn.disabled = false;
        }
    }

    async function rescanLibrary() {
        const btn = $('#dlna-rescan-btn');
        btn.disabled = true;
        btn.innerHTML = '<i class="fas fa-spinner fa-spin"></i> Skanowanie…';
        try {
            const data = await api('/dlna/rescan', { method: 'POST' });
            if (data.success) {
                toast('Skanowanie rozpoczęte', 'success');
                setTimeout(loadStatus, 3000);
            } else {
                toast(data.error || t('Skanowanie nie powiodło się'), 'error');
            }
        } catch (e) {
            toast(`${t('Błąd:')} ${e.message}`, 'error');
        } finally {
            btn.disabled = false;
            btn.innerHTML = '<i class="fas fa-sync-alt"></i> ' + t('Pełne skanowanie');
        }
    }

    /* ── Bind events ── */
    $('#dlna-install-btn').onclick = installDLNA;
    $('#dlna-save-btn').onclick = saveConfig;
    $('#dlna-rescan-btn').onclick = rescanLibrary;

    /* ── Init ── */
    loadStatus();
    loadConfig();
}
