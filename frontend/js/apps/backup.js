/* ═══════════════════════════════════════════════════════════
   EthOS — Backup & Restore  (v2 — enhanced UI)
   Kopia zapasowa — ścieżki, profile, USB/SSH, harmonogram,
   podgląd archiwów, edycja profili, logi w czasie rzecz.
   ═══════════════════════════════════════════════════════════ */

AppRegistry['backup'] = function (appDef) {
    createWindow('backup', {
        title: t('Kopia zapasowa'),
        icon: appDef.icon,
        iconColor: appDef.color,
        width: 1100,
        height: 750,
        onRender: (body) => renderBackupApp(body),
    });
};

function renderBackupApp(body) {
    const state = {
        paths: [], backups: [], history: [], profiles: [],
        sshServers: [], usbDrives: [], busy: false, tab: 'dashboard',
        logs: [],
    };

    /* ── Path prettifier: /data/home → /home, /data/mnt → /mnt etc. ── */
    function prettyPath(p) {
        if (!p) return '—';
        return p.replace(/^\/data\/run_media/, '/run/media')
                .replace(/^\/data\/home/, '/home')
                .replace(/^\/data\/mnt/, '/mnt') /* legacy */
                .replace(/^\/data\/media/, '/media')
                .replace(/^\/data\//, '/');
    }
    function prettyPathHtml(p) {
        var nice = prettyPath(p);
        var parts = nice.split('/');
        var name = parts.pop() || parts.pop() || nice;
        var dir = parts.join('/') + '/';
        return '<span class="bak-path-dir">' + dir + '</span><span class="bak-path-name">' + name + '</span>';
    }
    function destLabelHtml(dest) {
        if (!dest) return '<span class="bak-badge"><i class="fas fa-hdd"></i> Lokalnie</span>';
        if (dest.type === 'usb') {
            var usbName = dest.path ? dest.path.split('/').pop() : 'USB';
            return '<span class="bak-badge bak-badge-green"><i class="fas fa-usb"></i> USB: ' + usbName + '</span>';
        }
        if (dest.type === 'ssh') {
            var srv = state.sshServers.find(function(s) { return String(s.id) === String(dest.server_id); });
            var srvName = srv ? srv.name + ' (' + srv.host + ')' : 'SSH';
            return '<span class="bak-badge bak-badge-blue"><i class="fas fa-server"></i> ' + srvName + '</span>';
        }
        return '<span class="bak-badge"><i class="fas fa-hdd"></i> ' + dest.type + '</span>';
    }
    function formatSize(bytes) {
        if (!bytes || bytes === 0) return '0 B';
        if (bytes < 1024) return bytes + ' B';
        if (bytes < 1048576) return (bytes / 1024).toFixed(1) + ' KB';
        if (bytes < 1073741824) return (bytes / 1048576).toFixed(1) + ' MB';
        return (bytes / 1073741824).toFixed(2) + ' GB';
    }
    function formatDuration(sec) {
        if (!sec) return '—';
        if (sec < 60) return Math.round(sec) + 's';
        if (sec < 3600) return Math.floor(sec / 60) + 'min ' + Math.round(sec % 60) + 's';
        return Math.floor(sec / 3600) + 'h ' + Math.floor((sec % 3600) / 60) + 'min';
    }
    function parseBackupDate(filename) {
        var m = filename.match(/(\d{4})(\d{2})(\d{2})_(\d{2})(\d{2})(\d{2})/);
        if (m) return new Date(m[1], m[2]-1, m[3], m[4], m[5], m[6]);
        return null;
    }

    body.innerHTML = `
    <div class="bak-app">
        <div class="bak-sidebar">
            <div class="bak-sidebar-header">
                <div class="bak-sidebar-icon"><i class="fas fa-shield-alt"></i></div>
                <div>
                    <div class="bak-sidebar-title">${t('Kopie zapasowe')}</div>
                    <div class="bak-sidebar-sub" id="bak-status-indicator"></div>
                </div>
            </div>
            <nav class="bak-nav">
                <a class="bak-nav-item active" data-tab="dashboard"><i class="fas fa-th-large"></i><span>${t('Pulpit')}</span></a>
                <a class="bak-nav-item" data-tab="backup"><i class="fas fa-plus-circle"></i><span>${t('Nowa kopia')}</span></a>
                <a class="bak-nav-item" data-tab="snapshots"><i class="fas fa-camera-retro"></i><span>${t('Punkty przywracania')}</span></a>
                <div class="bak-nav-sep"></div>
                <a class="bak-nav-item" data-tab="restore"><i class="fas fa-undo"></i><span>${t('Przywracanie')}</span></a>
                <a class="bak-nav-item" data-tab="history"><i class="fas fa-history"></i><span>${t('Historia')}</span></a>
                <div class="bak-nav-sep"></div>
                <a class="bak-nav-item" data-tab="profiles"><i class="fas fa-bookmark"></i><span>${t('Harmonogramy')}</span></a>
                <a class="bak-nav-item" data-tab="destination"><i class="fas fa-cog"></i><span>${t('Ustawienia')}</span></a>
            </nav>
        </div>
        <div class="bak-content">

        <!-- ═══ DASHBOARD ═══ -->
        <div class="bak-panel active" id="bak-panel-dashboard">
            <div class="bak-dash-welcome">
                <h2><i class="fas fa-shield-alt bak-icon-accent"></i> ${t('Centrum kopii zapasowych')}</h2>
                <p>${t('Zabezpiecz swoje dane. Wybierz co chcesz zrobić:')}</p>
            </div>
            <!-- Shield status widget -->
            <div class="bak-shield-status" id="bak-shield-status">
                <div class="bak-shield-icon" id="bak-shield-icon"><i class="fas fa-shield-alt"></i></div>
                <div class="bak-shield-info">
                    <div class="bak-shield-label" id="bak-shield-label">${t('Sprawdzanie...')}</div>
                    <div class="bak-shield-detail" id="bak-shield-detail"></div>
                </div>
            </div>
            <div class="bak-dash-grid">
                <div class="bak-dash-card bak-dash-green" data-goto="backup">
                    <div class="bak-dash-card-icon"><i class="fas fa-folder-plus"></i></div>
                    <div class="bak-dash-card-text">
                        <strong>${t('Nowa kopia zapasowa')}</strong>
                        <span>${t('Wybierz foldery i zabezpiecz swoje pliki')}</span>
                    </div>
                    <i class="fas fa-chevron-right bak-dash-arrow"></i>
                </div>
                <div class="bak-dash-card bak-dash-blue" data-goto="snapshots">
                    <div class="bak-dash-card-icon"><i class="fas fa-camera-retro"></i></div>
                    <div class="bak-dash-card-text">
                        <strong>${t('Punkt przywracania')}</strong>
                        <span>${t('Cofnij NAS do poprzedniego stanu')}</span>
                    </div>
                    <i class="fas fa-chevron-right bak-dash-arrow"></i>
                </div>
                <div class="bak-dash-card bak-dash-orange" data-goto="restore">
                    <div class="bak-dash-card-icon"><i class="fas fa-undo"></i></div>
                    <div class="bak-dash-card-text">
                        <strong>${t('Przywróć dane')}</strong>
                        <span>${t('Odzyskaj pliki z wcześniejszej kopii')}</span>
                    </div>
                    <i class="fas fa-chevron-right bak-dash-arrow"></i>
                </div>
                <div class="bak-dash-card bak-dash-purple" data-goto="profiles">
                    <div class="bak-dash-card-icon"><i class="fas fa-calendar-check"></i></div>
                    <div class="bak-dash-card-text">
                        <strong>${t('Automatyczne kopie')}</strong>
                        <span>${t('Profile i harmonogramy backupów')}</span>
                    </div>
                    <i class="fas fa-chevron-right bak-dash-arrow"></i>
                </div>
            </div>
            <div class="bak-dash-summary" id="bak-dash-summary"></div>
        </div>

        <!-- ═══ PROFILES TAB ═══ -->
        <div class="bak-panel" id="bak-panel-profiles">
            <div class="bak-section-header">
                <h3><i class="fas fa-bookmark"></i> Profile backupów</h3>
                <div class="bak-row">
                    <button class="fm-toolbar-btn btn-green" id="bak-profile-new"><i class="fas fa-plus"></i> Nowy profil</button>
                    <button class="fm-toolbar-btn" id="bak-profile-export" title="Eksportuj profile"><i class="fas fa-file-export"></i> Eksportuj</button>
                    <button class="fm-toolbar-btn" id="bak-profile-import" title="Importuj profile"><i class="fas fa-file-import"></i> Importuj</button>
                    <input type="file" id="bak-profile-import-file" accept=".json" class="bak-hidden">
                </div>
            </div>
            <div id="bak-profiles-list"></div>

            <h3 class="bak-mt24"><i class="fas fa-clock"></i> Zaplanowane backupy</h3>
            <div id="bak-scheduled-list"></div>
        </div>

        <!-- ═══ BACKUP TAB ═══ -->
        <div class="bak-panel" id="bak-panel-backup">
            <h3><i class="fas fa-folder-plus"></i> ${t('Nowa kopia zapasowa')}</h3>
            <p class="bak-panel-desc"><i class="fas fa-info-circle"></i> ${t('Wybierz foldery do zabezpieczenia, a następnie wskaż gdzie zapisać kopię.')}</p>

            <div class="bak-step">
                <div class="bak-step-num">1</div>
                <div class="bak-step-body">
                    <div class="bak-step-label">${t('Wybierz foldery do zabezpieczenia')}</div>
                    <div class="bak-paths-list" id="bak-paths"></div>
                    <div class="bak-mt8">
                        <button class="fm-toolbar-btn btn-green" id="bak-browse-btn"><i class="fas fa-folder-plus"></i> Dodaj folder</button>
                    </div>
                </div>
            </div>

            <div class="bak-step">
                <div class="bak-step-num">2</div>
                <div class="bak-step-body">
                    <div class="bak-step-label">${t('Gdzie zapisać kopię?')}</div>
                <div class="bak-dest-row">
                    <select id="bak-dest-type" class="fm-input bak-w-auto">
                        <option value="local">Lokalnie</option>
                        <option value="usb">USB</option>
                        <option value="ssh">SSH</option>
                    </select>
                    <select id="bak-dest-usb" class="fm-input hidden bak-w-usb-select"></select>
                    <button class="fm-toolbar-btn hidden" id="bak-dest-usb-browse" title="Przeglądaj USB"><i class="fas fa-folder-open"></i></button>
                    <select id="bak-dest-ssh" class="fm-input hidden bak-w-auto"></select>
                </div>
                <div class="bak-dest-path-display hidden" id="bak-dest-path-display">
                    <i class="fas fa-folder bak-icon-accent"></i>
                    <span id="bak-dest-path-text"></span>
                    <button class="fm-toolbar-btn btn-red btn-sm" id="bak-dest-path-clear" title="Resetuj"><i class="fas fa-times"></i></button>
                </div>
                </div>
            </div>

            <div class="bak-step">
                <div class="bak-step-num">3</div>
                <div class="bak-step-body">
                    <div class="bak-step-label">${t('Dodatkowe opcje')} <span class="bak-optional">${t('(opcjonalnie)')}</span></div>
            <div class="bak-row-wrap">
                <label class="bak-row" title="${t('Kopiuje tylko zmiany od ostatniego razu — szybsze i mniejsze')}"><input type="checkbox" id="bak-adhoc-incr"> ${t('Tylko zmiany od ostatniego razu')}</label>
                <label class="bak-row">${t('Zachowaj kopii:')} <input type="number" id="bak-adhoc-retention" class="fm-input bak-w70" value="0" min="0"> <span class="bak-hint">${t('(0 = bez limitu, starsze usuwane automatycznie)')}</span></label>
            </div>
                </div>
            </div>

            <div class="bak-start-area">
                <button class="printer-btn bak-shrink0" id="bak-start-btn"><i class="fas fa-play"></i> Rozpocznij backup</button>
                <span class="bak-caption">Kopia zostanie utworzona w wybranej lokalizacji</span>
            </div>
            <!-- Progress panel -->
            <div class="bak-progress hidden" id="bak-progress">
                <div class="bak-progress-header">
                    <span id="bak-progress-stage" class="bak-badge">Archiwizacja</span>
                    <span id="bak-progress-pct" class="bak-fw600">0%</span>
                </div>
                <div class="res-bar bak-bar-h8"><div class="res-bar-fill bak-progress-fill" id="bak-progress-bar"></div></div>
                <div class="bak-progress-text" id="bak-progress-text">—</div>
                <div class="bak-progress-info" id="bak-progress-info"></div>
                <details class="bak-log-details" id="bak-log-section">
                    <summary><i class="fas fa-terminal"></i> Logi</summary>
                    <div class="bak-log-viewer" id="bak-log-viewer"></div>
                </details>
                <div class="bak-row-mt8">
                    <button class="fm-toolbar-btn btn-red btn-sm" id="bak-cancel-btn"><i class="fas fa-stop"></i> Anuluj</button>
                    <button class="fm-toolbar-btn btn-sm bak-ml-auto" id="bak-progress-close"><i class="fas fa-times"></i> Zamknij</button>
                </div>
            </div>
        </div>

        <!-- ═══ RESTORE TAB ═══ -->
        <div class="bak-panel" id="bak-panel-restore">
            <h3><i class="fas fa-undo"></i> ${t('Przywracanie danych')}</h3>
            <p class="bak-panel-desc"><i class="fas fa-info-circle"></i> ${t('Przeglądaj istniejące kopie zapasowe i przywróć wybrane pliki.')}</p>
            <div id="bak-backups-list"></div>
            <div class="bak-restore-opts bak-card-section">
                <div class="bak-heading13-mb"><i class="fas fa-crosshairs"></i> ${t('Gdzie przywrócić pliki?')}</div>
                <label class="bak-radio-label">
                    <input type="radio" name="bak-restore-mode" value="safe" id="bak-restore-mode-safe" checked>
                    <span><strong>${t('Bezpieczne — do nowego folderu')}</strong><br><span class="bak-small-muted">${t('Nie nadpisze istniejących plików')}</span></span>
                </label>
                <label class="bak-radio-label-last">
                    <input type="radio" name="bak-restore-mode" value="original" id="bak-restore-mode-original">
                    <span><strong>${t('Oryginalna lokalizacja')}</strong><br><span class="bak-small-warning">${t('⚠ Nadpisze nowsze pliki starszą wersją!')}</span></span>
                </label>
                <div id="bak-restore-safe-path" class="bak-mt8">
                    <input type="text" id="bak-restore-target" class="fm-input bak-w-full" value="">
                    <span class="bak-small-muted">${t('Folder zostanie utworzony automatycznie')}</span>
                </div>
            </div>
        </div>

        <!-- ═══ DESTINATION TAB ═══ -->
        <div class="bak-panel" id="bak-panel-destination">
            <h3><i class="fas fa-cog"></i> ${t('Ustawienia celów kopii')}</h3>
            <p class="bak-panel-desc"><i class="fas fa-info-circle"></i> ${t('Skonfiguruj dyski USB i zdalne serwery, na które można wysyłać kopie zapasowe i punkty przywracania.')}</p>

            <h4 class="bak-mt8"><i class="fas fa-usb bak-icon-accent"></i> ${t('Dyski USB')}</h4>
            <button class="fm-toolbar-btn bak-mb8" id="bak-usb-refresh"><i class="fas fa-sync-alt"></i> ${t('Odśwież')}</button>
            <div id="bak-usb-list"></div>

            <h4 class="bak-mt24"><i class="fas fa-server bak-icon-accent"></i> ${t('Zdalne serwery')}</h4>
            <p class="bak-desc-muted">${t('Inne serwery lub NAS-y, na które można wysyłać kopie przez szyfrowane połączenie.')}</p>
            <button class="fm-toolbar-btn btn-green bak-mb8" id="bak-ssh-add"><i class="fas fa-plus"></i> ${t('Dodaj serwer')}</button>
            <div id="bak-ssh-list"></div>
            <div class="bak-ssh-form hidden" id="bak-ssh-form">
                <h4>${t('Nowe połączenie zdalne')}</h4>
                <div class="storage-form-row"><label>Nazwa:</label><input type="text" id="bak-ssh-name" class="fm-input"></div>
                <div class="storage-form-row"><label>Host:</label><input type="text" id="bak-ssh-host" class="fm-input"><label>Port:</label><input type="number" id="bak-ssh-port" class="fm-input bak-w80" value="22"></div>
                <div class="storage-form-row"><label>Użytkownik:</label><input type="text" id="bak-ssh-user" class="fm-input"></div>
                <div class="storage-form-row"><label>Uwierzytelnianie:</label>
                    <select id="bak-ssh-auth-type" class="fm-input bak-w180">
                        <option value="password">Hasło</option>
                        <option value="key">Klucz SSH</option>
                    </select>
                </div>
                <div id="bak-ssh-auth-pw">
                    <div class="storage-form-row"><label>Hasło:</label><input type="password" id="bak-ssh-pw" class="fm-input"></div>
                </div>
                <div id="bak-ssh-auth-key" class="hidden">
                    <div class="storage-form-row"><label>Klucz SSH:</label><select id="bak-ssh-key-select" class="fm-input"><option value="">-- wybierz klucz --</option></select></div>
                </div>
                <div class="storage-form-row"><label>Zdalny katalog:</label><input type="text" id="bak-ssh-path" class="fm-input" value="/backups"></div>
                <div class="storage-form-row">
                    <button class="fm-toolbar-btn btn-green" id="bak-ssh-save"><i class="fas fa-save"></i> Zapisz</button>
                    <button class="fm-toolbar-btn" id="bak-ssh-test"><i class="fas fa-plug"></i> Testuj</button>
                    <button class="fm-toolbar-btn" id="bak-ssh-cancel"><i class="fas fa-times"></i> Anuluj</button>
                </div>
            </div>
        </div>

        <!-- ═══ HISTORY TAB ═══ -->
        <div class="bak-panel" id="bak-panel-history">
            <h3><i class="fas fa-history"></i> ${t('Historia operacji')}</h3>
            <p class="bak-panel-desc"><i class="fas fa-info-circle"></i> ${t('Przegląd wszystkich wykonanych kopii zapasowych i przywróceń.')}</p>
            <div id="bak-history-list"></div>
        </div>

        <!-- ═══ SNAPSHOTS TAB ═══ -->
        <div class="bak-panel" id="bak-panel-snapshots">
            <div class="bak-section-header">
                <h3><i class="fas fa-camera-retro"></i> ${t('Punkty przywracania')}</h3>
                <div class="bak-row-sm">
                    <button class="fm-toolbar-btn" id="snap-discover-btn" title="${t('Wykryj inne NASy w sieci')}"><i class="fas fa-satellite-dish"></i> ${t('Wykryj NAS')}</button>
                    <button class="fm-toolbar-btn" id="snap-remote-btn" title="${t('Zdalne punkty przywracania')}"><i class="fas fa-cloud"></i> ${t('Zdalne')}</button>
                    <button class="fm-toolbar-btn" id="snap-import-btn" title="${t('Importuj z pliku')}"><i class="fas fa-file-import"></i> ${t('Importuj')}</button>
                    <button class="fm-toolbar-btn btn-green" id="snap-create-btn"><i class="fas fa-plus"></i> ${t('Nowy punkt')}</button>
                    <input type="file" id="snap-import-file" accept=".tar.gz,.gz" class="bak-hidden">
                </div>
            </div>
            <!-- Snapshot space usage widget -->
            <div class="bak-snap-space bak-hidden" id="bak-snap-space">
                <div class="bak-row-between-mb">
                    <span class="bak-label12"><i class="fas fa-database bak-mr4"></i>${t('Zajętość punktów przywracania')}</span>
                    <span id="bak-snap-space-text" class="bak-small12-muted"></span>
                </div>
                <div class="res-bar bak-bar-h6"><div class="res-bar-fill bak-progress-fill" id="bak-snap-space-bar"></div></div>
                <div id="bak-snap-space-hint" class="bak-small-muted-mt"></div>
            </div>
            <p class="bak-panel-desc"><i class="fas fa-info-circle"></i>
                ${t('Punkt przywracania zapisuje stan NAS — konfigurację, Docker, wolumeny i ustawienia systemu.')}
                ${t('Przywróć NAS do zapisanego stanu, prześlij punkt na inny NAS lub zaimportuj.')}
            </p>
            <div id="snap-progress" class="hidden bak-progress-panel">
                <div class="bak-row-between-mb">
                    <span id="snap-progress-msg" class="bak-fw600">Tworzenie...</span>
                    <span id="snap-progress-pct" class="bak-fw600-accent">0%</span>
                </div>
                <div class="bak-bar-track">
                    <div id="snap-progress-bar" class="bak-snap-progress-fill"></div>
                </div>
                <pre id="snap-progress-log" class="bak-progress-log"></pre>
            </div>
            <div id="snap-list"></div>

            <!-- Received snapshots refresh (hidden control for programmatic use) -->
            <div id="snap-received-section" class="bak-hidden">
                <button class="fm-toolbar-btn" id="snap-received-refresh" title="Odśwież"><i class="fas fa-sync-alt"></i></button>
            </div>
        </div>
        </div><!-- /bak-content -->

        <!-- ═══ Received snapshot restore modal ═══ -->
        <div class="bak-modal hidden" id="snap-received-restore-modal">
            <div class="bak-modal-content bak-modal-520">
                <div class="bak-modal-header">
                    <h3><i class="fas fa-undo bak-text-amber"></i> ${t('Przywróć otrzymany punkt')}</h3>
                    <button class="fm-toolbar-btn" id="snap-rcv-restore-close"><i class="fas fa-times"></i></button>
                </div>
                <div class="bak-p16">
                    <div id="snap-rcv-restore-info" class="bak-info-panel"></div>
                    <div class="bak-heading13-mb">${t('Co przywrócić:')}</div>
                    <div class="bak-col-gap8">
                        <label class="bak-check-label"><input type="checkbox" id="snap-rcv-rst-ethos" checked> <i class="fas fa-server"></i> ${t('Ustawienia EthOS')}</label>
                        <label class="bak-check-label"><input type="checkbox" id="snap-rcv-rst-system" checked> <i class="fas fa-cog"></i> ${t('Ustawienia systemu')}</label>
                        <label class="bak-check-label"><input type="checkbox" id="snap-rcv-rst-docker" checked> <i class="fab fa-docker"></i> ${t('Aplikacje Docker')}</label>
                        <label class="bak-check-label"><input type="checkbox" id="snap-rcv-rst-volumes" checked> <i class="fas fa-database"></i> ${t('Dane aplikacji Docker')}</label>
                    </div>
                    <div class="bak-warn-panel">
                        <i class="fas fa-exclamation-triangle"></i> <b>${t('Uwaga:')}</b> ${t('Przywracanie nadpisze obecną konfigurację! Obecne dane EthOS zostaną skopiowane do')} <code>data.pre-restore/</code>.
                    </div>
                </div>
                <div class="bak-modal-footer">
                    <button class="fm-toolbar-btn" id="snap-rcv-restore-cancel">${t('Anuluj')}</button>
                    <button class="fm-toolbar-btn bak-btn-danger" id="snap-rcv-restore-go"><i class="fas fa-undo"></i> ${t('Przywróć')}</button>
                </div>
            </div>
        </div>

        <!-- ═══ Snapshot create modal ═══ -->
        <div class="bak-modal hidden" id="snap-create-modal">
            <div class="bak-modal-content bak-modal-520">
                <div class="bak-modal-header">
                    <h3><i class="fas fa-plus-circle"></i> ${t('Nowy punkt przywracania')}</h3>
                    <button class="fm-toolbar-btn" id="snap-create-close"><i class="fas fa-times"></i></button>
                </div>
                <div class="bak-form-body">
                    <div>
                        <label class="bak-heading13">${t('Nazwa (opcjonalna)')}</label>
                        <input type="text" class="fm-input bak-input-full-mt" id="snap-label" placeholder="${t('np. Przed aktualizacją...')}">
                    </div>
                    <div class="bak-heading13-tight">${t('Co uwzględnić:')}</div>
                    <label class="bak-check-label"><input type="checkbox" id="snap-inc-ethos" checked> <i class="fas fa-server"></i> ${t('Ustawienia EthOS (konta, konfiguracja, baza danych)')}</label>
                    <label class="bak-check-label"><input type="checkbox" id="snap-inc-system" checked> <i class="fas fa-cog"></i> ${t('Ustawienia systemu (sieć, certyfikaty SSL, hostname)')}</label>
                    <label class="bak-check-label"><input type="checkbox" id="snap-inc-docker" checked> <i class="fab fa-docker"></i> ${t('Aplikacje Docker (projekty i konfiguracja)')}</label>
                    <label class="bak-check-label"><input type="checkbox" id="snap-inc-volumes" checked> <i class="fas fa-database"></i> ${t('Dane aplikacji Docker (może być duże!)')}</label>
                    <div class="bak-section-divider">
                        <label class="bak-heading13">${t('Dodatkowy cel (opcjonalnie):')}</label>
                        <select id="snap-dest-type" class="fm-input bak-mt4">
                            <option value="local">${t('Tylko lokalnie')}</option>
                            <option value="usb">${t('Kopia na USB')}</option>
                        </select>
                        <select id="snap-dest-usb" class="fm-input hidden bak-mt6"></select>
                    </div>
                </div>
                <div class="bak-modal-footer">
                    <button class="fm-toolbar-btn" id="snap-create-cancel">Anuluj</button>
                    <button class="fm-toolbar-btn btn-green" id="snap-create-go"><i class="fas fa-camera-retro"></i> ${t('Utwórz punkt przywracania')}</button>
                </div>
            </div>
        </div>

        <!-- ═══ Snapshot restore modal ═══ -->
        <div class="bak-modal hidden" id="snap-restore-modal">
            <div class="bak-modal-content bak-modal-520">
                <div class="bak-modal-header">
                    <h3><i class="fas fa-undo"></i> ${t('Przywracanie systemu z punktu')}</h3>
                    <button class="fm-toolbar-btn" id="snap-restore-close"><i class="fas fa-times"></i></button>
                </div>
                <div class="bak-p16">
                    <div id="snap-restore-info" class="bak-info-panel"></div>
                    <div class="bak-heading13-mb">${t('Co przywrócić:')}</div>
                    <div class="bak-col-gap8">
                        <label class="bak-check-label"><input type="checkbox" id="snap-rst-ethos" checked> <i class="fas fa-server"></i> ${t('Ustawienia EthOS')}</label>
                        <label class="bak-check-label"><input type="checkbox" id="snap-rst-system" checked> <i class="fas fa-cog"></i> ${t('Ustawienia systemu')}</label>
                        <label class="bak-check-label"><input type="checkbox" id="snap-rst-docker" checked> <i class="fab fa-docker"></i> ${t('Aplikacje Docker')}</label>
                        <label class="bak-check-label"><input type="checkbox" id="snap-rst-volumes" checked> <i class="fas fa-database"></i> ${t('Dane aplikacji Docker')}</label>
                    </div>
                    <div class="bak-warn-panel">
                        <i class="fas fa-exclamation-triangle"></i> <b>${t('Uwaga:')}</b> ${t('Przywracanie nadpisze obecną konfigurację! Obecne dane EthOS zostaną skopiowane do')} <code>data.pre-restore/</code>.
                    </div>
                    <div class="bak-mt12">
                        <label class="bak-label12-block">${t('Wpisz PRZYWRÓĆ aby potwierdzić:')}</label>
                        <input type="text" id="snap-restore-confirm-input" class="fm-input bak-input-confirm" placeholder="PRZYWRÓĆ" autocomplete="off">
                    </div>
                </div>
                <div class="bak-modal-footer">
                    <button class="fm-toolbar-btn" id="snap-restore-cancel">${t('Anuluj')}</button>
                    <button class="fm-toolbar-btn bak-btn-danger-disabled" id="snap-restore-go"><i class="fas fa-undo"></i> ${t('Przywróć')}</button>
                </div>
            </div>
        </div>

        <!-- ═══ Snapshot transfer modal ═══ -->
        <div class="bak-modal hidden" id="snap-browse-modal">
            <div class="bak-modal-content bak-modal-650">
                <div class="bak-modal-header">
                    <h3><i class="fas fa-folder-open"></i> ${t('Zawartość punktu przywracania')}</h3>
                    <button class="fm-toolbar-btn" id="snap-browse-close"><i class="fas fa-times"></i></button>
                </div>
                <div class="bak-p16">
                    <div id="snap-browse-info" class="bak-info-panel"></div>
                    <div id="snap-browse-tree" class="bak-scroll-mono"></div>
                </div>
            </div>
        </div>

        <!-- ═══ Snapshot transfer modal (send) ═══ -->
        <div class="bak-modal hidden" id="snap-transfer-modal">
            <div class="bak-modal-content bak-modal-500">
                <div class="bak-modal-header">
                    <h3><i class="fas fa-share"></i> ${t('Wyślij punkt przywracania')}</h3>
                    <button class="fm-toolbar-btn" id="snap-transfer-close"><i class="fas fa-times"></i></button>
                </div>
                <div class="bak-p16">
                    <div id="snap-transfer-info" class="bak-info-panel"></div>
                    <div class="bak-heading13-mb">${t('Docelowy serwer:')}</div>
                    <select id="snap-transfer-server" class="bak-textarea">
                        <option value="">-- wybierz serwer --</option>
                    </select>
                    <div id="snap-transfer-discovered" class="bak-mt10"></div>
                    <div class="bak-info-note">
                        <i class="fas fa-info-circle"></i> ${t('Punkt przywracania zostanie przesłany na zdalny NAS i automatycznie rozpakowany.')}
                        ${t('Dodaj serwer z wykrytych NAS-ów lub skonfiguruj ręcznie w zakładce')} <b>${t('Ustawienia')}</b>.
                    </div>
                </div>
                <div class="bak-modal-footer">
                    <button class="fm-toolbar-btn" id="snap-transfer-cancel">${t('Anuluj')}</button>
                    <button class="fm-toolbar-btn btn-green" id="snap-transfer-go"><i class="fas fa-share"></i> ${t('Wyślij')}</button>
                </div>
            </div>
        </div>

        <!-- ═══ Remote snapshots modal ═══ -->
        <div class="bak-modal hidden" id="snap-remote-modal">
            <div class="bak-modal-content bak-modal-620">
                <div class="bak-modal-header">
                    <h3><i class="fas fa-cloud"></i> ${t('Zdalne punkty przywracania')}</h3>
                    <button class="fm-toolbar-btn" id="snap-remote-close"><i class="fas fa-times"></i></button>
                </div>
                <div class="bak-p16">
                    <div class="bak-row-mb14">
                        <select id="snap-remote-server" class="bak-input-text">
                            <option value="">-- wybierz serwer --</option>
                        </select>
                        <button class="fm-toolbar-btn btn-green" id="snap-remote-load"><i class="fas fa-sync"></i> ${t('Załaduj')}</button>
                    </div>
                    <div id="snap-remote-list" class="bak-scroll-400">
                        <div class="bak-empty">${t('Wybierz serwer i kliknij „Załaduj”')}</div>
                    </div>
                </div>
            </div>
        </div>

        <!-- ═══ NAS discovery modal ═══ -->
        <div class="bak-modal hidden" id="snap-discover-modal">
            <div class="bak-modal-content bak-modal-620">
                <div class="bak-modal-header">
                    <h3><i class="fas fa-satellite-dish"></i> ${t('Wykryte instancje EthOS')}</h3>
                    <button class="fm-toolbar-btn" id="snap-discover-close"><i class="fas fa-times"></i></button>
                </div>
                <div class="bak-p16">
                    <p class="bak-desc-muted-lg">
                        ${t('Skanowanie sieci w poszukiwaniu innych NAS-ów z EthOS.')}
                        ${t('Wykryte urządzenia można dodać jako serwer docelowy do wysyłania punktów przywracania.')}
                    </p>
                    <div class="bak-row-mb14">
                        <button class="fm-toolbar-btn btn-green bak-flex-none" id="snap-discover-scan"><i class="fas fa-search"></i> ${t('Skanuj sieć')}</button>
                        <div id="snap-discover-status" class="bak-flex1-muted"></div>
                    </div>
                    <div id="snap-discover-list" class="bak-scroll-400">
                        <div class="bak-empty"><i class="fas fa-satellite-dish bak-empty-icon-sm"></i>${t('Kliknij „Skanuj sieć” aby wyszukać inne NAS-y')}</div>
                    </div>
                </div>
            </div>
        </div>

        <!-- ═══ Add discovered NAS as SSH server modal ═══ -->
        <div class="bak-modal hidden" id="snap-addnas-modal">
            <div class="bak-modal-content bak-modal-460">
                <div class="bak-modal-header">
                    <h3><i class="fas fa-plus-circle"></i> Dodaj NAS jako cel</h3>
                    <button class="fm-toolbar-btn" id="snap-addnas-close"><i class="fas fa-times"></i></button>
                </div>
                <div class="bak-p16">
                    <div id="snap-addnas-info" class="bak-info-panel"></div>
                    <div class="bak-col-gap10">
                        <div>
                            <label class="bak-label12-block">Użytkownik SSH:</label>
                            <input type="text" id="snap-addnas-user" placeholder="np. nasadmin" class="bak-textarea">
                        </div>
                        <div>
                            <label class="bak-label12-block">Hasło:</label>
                            <input type="password" id="snap-addnas-pass" placeholder="${t('Hasło SSH')}" class="bak-textarea">
                        </div>
                        <div>
                            <label class="bak-label12-block">Ścieżka zdalna (backup dir):</label>
                            <input type="text" id="snap-addnas-path" value="/backups" class="bak-textarea">
                        </div>
                    </div>
                </div>
                <div class="bak-modal-footer">
                    <button class="fm-toolbar-btn" id="snap-addnas-cancel">Anuluj</button>
                    <button class="fm-toolbar-btn btn-green" id="snap-addnas-go"><i class="fas fa-check"></i> Dodaj i testuj</button>
                </div>
            </div>
        </div>

        <!-- ═══ MODALS ═══ -->
        <!-- Preview modal -->
        <div class="bak-modal hidden" id="bak-preview-modal">
            <div class="bak-modal-content bak-modal-650">
                <div class="bak-modal-header">
                    <h3><i class="fas fa-search"></i> Podgląd kopii</h3>
                    <button class="fm-toolbar-btn" id="bak-preview-close"><i class="fas fa-times"></i></button>
                </div>
                <div class="bak-preview-body bak-scroll-body" id="bak-preview-body">
                    <p class="bak-text-muted">Ładowanie...</p>
                </div>
                <div class="bak-modal-footer">
                    <button class="fm-toolbar-btn btn-green" id="bak-preview-restore"><i class="fas fa-undo"></i> Przywróć tę kopię</button>
                </div>
            </div>
        </div>

        <!-- Profile editor modal -->
        <div class="bak-modal hidden" id="bak-profile-modal">
            <div class="bak-modal-content bak-modal-600">
                <div class="bak-modal-header">
                    <h3 id="bak-pm-title"><i class="fas fa-bookmark"></i> Nowy profil</h3>
                    <button class="fm-toolbar-btn" id="bak-pm-close"><i class="fas fa-times"></i></button>
                </div>
                <div class="bak-scroll-body">
                    <div class="storage-form-row"><label>Nazwa:</label><input type="text" id="bak-pm-name" class="fm-input"></div>

                    <label class="bak-block-mt12"><strong>Ścieżki:</strong></label>
                    <div class="bak-pm-paths" id="bak-pm-paths"></div>
                    <div class="bak-mt6">
                        <button class="fm-toolbar-btn btn-green" id="bak-pm-browse"><i class="fas fa-folder-plus"></i> Dodaj folder</button>
                    </div>

                    <div class="storage-form-row bak-mt14">
                        <label>Cel:</label>
                        <select id="bak-pm-dest" class="fm-input bak-w-auto">
                            <option value="local">Lokalnie</option>
                            <option value="usb">USB</option>
                            <option value="ssh">SSH</option>
                        </select>
                        <select id="bak-pm-dest-usb" class="fm-input hidden bak-w-usb-select"></select>
                        <button class="fm-toolbar-btn hidden" id="bak-pm-dest-usb-browse" title="Przeglądaj USB"><i class="fas fa-folder-open"></i></button>
                        <select id="bak-pm-dest-ssh" class="fm-input hidden bak-w-auto"></select>
                    </div>
                    <div class="bak-dest-path-display hidden" id="bak-pm-dest-path-display">
                        <i class="fas fa-folder bak-icon-accent"></i>
                        <span id="bak-pm-dest-path-text"></span>
                        <button class="fm-toolbar-btn btn-red btn-sm" id="bak-pm-dest-path-clear" title="Resetuj"><i class="fas fa-times"></i></button>
                    </div>

                    <div class="storage-form-row bak-mt10">
                        <label>Harmonogram:</label>
                        <select id="bak-pm-sched-type" class="fm-input bak-w-auto">
                            <option value="manual">Ręczny</option>
                            <option value="daily">Codziennie</option>
                            <option value="weekly">Co tydzień</option>
                        </select>
                        <label>Godzina:</label>
                        <input type="time" id="bak-pm-sched-time" class="fm-input bak-w120" value="03:00">
                    </div>
                    <div class="bak-pm-days hidden bak-tag-row" id="bak-pm-days">
                        <label class="bak-day-chip"><input type="checkbox" value="0"> Pn</label>
                        <label class="bak-day-chip"><input type="checkbox" value="1"> Wt</label>
                        <label class="bak-day-chip"><input type="checkbox" value="2"> Śr</label>
                        <label class="bak-day-chip"><input type="checkbox" value="3"> Cz</label>
                        <label class="bak-day-chip"><input type="checkbox" value="4"> Pt</label>
                        <label class="bak-day-chip"><input type="checkbox" value="5"> So</label>
                        <label class="bak-day-chip"><input type="checkbox" value="6"> Nd</label>
                    </div>

                    <div class="storage-form-row bak-mt10">
                        <label>Retencja (kopii):</label>
                        <input type="number" id="bak-pm-retention" class="fm-input bak-w80" value="3" min="0">
                        <label class="storage-check"><input type="checkbox" id="bak-pm-incr"> Przyrostowy</label>
                    </div>

                    <div class="storage-form-row bak-mt10">
                        <label class="storage-check"><input type="checkbox" id="bak-pm-encrypt"> <i class="fas fa-lock"></i> Szyfruj backup (AES-256)</label>
                    </div>
                    <div class="hidden" id="bak-pm-encrypt-opts">
                        <div class="storage-form-row bak-mt10">
                            <label>Tryb klucza:</label>
                            <label class="storage-check"><input type="radio" name="bak-pm-enc-mode" value="passphrase" id="bak-pm-enc-passphrase" checked> <i class="fas fa-keyboard"></i> Hasło (wpisywane)</label>
                            <label class="storage-check"><input type="radio" name="bak-pm-enc-mode" value="key" id="bak-pm-enc-key"> <i class="fas fa-key"></i> Klucz automatyczny</label>
                        </div>
                        <div class="bak-encrypt-warning" id="bak-pm-encrypt-warning">
                            <i class="fas fa-exclamation-triangle"></i>
                            <strong>Zapamiętaj hasło!</strong> Bez niego backup jest bezużyteczny — nie ma możliwości odzyskania danych.
                        </div>
                        <div class="bak-encrypt-info hidden" id="bak-pm-key-info">
                            <i class="fas fa-info-circle"></i>
                            Klucz zostanie wygenerowany automatycznie i zapisany (zaplanowane backupy działają). Zapamiętaj klucz — zostanie pokazany raz po zapisaniu.
                        </div>
                    </div>
                </div>
                <div class="bak-modal-footer bak-gap8">
                    <button class="fm-toolbar-btn btn-green" id="bak-pm-save"><i class="fas fa-save"></i> Zapisz</button>
                    <button class="fm-toolbar-btn" id="bak-pm-cancel"><i class="fas fa-times"></i> Anuluj</button>
                </div>
            </div>
        </div>
    </div>

    <!-- Passphrase modal for encrypted backup/restore -->
    <div class="bak-modal hidden" id="bak-passphrase-modal">
        <div class="bak-modal-content bak-modal-400">
            <div class="bak-modal-header">
                <h3 id="bak-passphrase-title"><i class="fas fa-lock"></i> Hasło szyfrowania</h3>
                <button class="fm-toolbar-btn" id="bak-passphrase-close"><i class="fas fa-times"></i></button>
            </div>
            <div class="bak-scroll-body">
                <div class="bak-encrypt-warning bak-mb12">
                    <i class="fas fa-exclamation-triangle"></i>
                    <strong>Zapamiętaj hasło!</strong> Bez niego backup jest bezużyteczny — nie ma możliwości odzyskania danych.
                </div>
                <div class="storage-form-row">
                    <label>Hasło:</label>
                    <input type="password" id="bak-passphrase-input" class="fm-input" autocomplete="new-password" placeholder="Hasło szyfrowania...">
                </div>
                <div class="storage-form-row" id="bak-passphrase-confirm-row">
                    <label>Potwierdź:</label>
                    <input type="password" id="bak-passphrase-confirm" class="fm-input" autocomplete="new-password" placeholder="Powtórz hasło...">
                </div>
            </div>
            <div class="bak-modal-footer bak-gap8">
                <button class="fm-toolbar-btn btn-green" id="bak-passphrase-ok"><i class="fas fa-check"></i> Potwierdź</button>
                <button class="fm-toolbar-btn" id="bak-passphrase-cancel"><i class="fas fa-times"></i> Anuluj</button>
            </div>
        </div>
    </div>

    <!-- Generated key display modal -->
    <div class="bak-modal hidden" id="bak-genkey-modal">
        <div class="bak-modal-content bak-modal-480">
            <div class="bak-modal-header">
                <h3><i class="fas fa-key"></i> Klucz szyfrowania — zapisz!</h3>
            </div>
            <div class="bak-scroll-body">
                <div class="bak-encrypt-warning bak-mb12">
                    <i class="fas fa-exclamation-triangle"></i>
                    <strong>Zapisz ten klucz w bezpiecznym miejscu!</strong> Bez niego nie będziesz mógł przywrócić zaszyfrowanych backupów. Klucz jest pokazywany tylko raz po wygenerowaniu.
                </div>
                <div class="storage-form-row">
                    <label>Klucz szyfrowania:</label>
                    <div id="bak-genkey-value" class="bak-key-display"></div>
                </div>
                <div class="storage-form-row bak-mt10">
                    <button class="fm-toolbar-btn" id="bak-genkey-copy"><i class="fas fa-copy"></i> Kopiuj klucz</button>
                </div>
            </div>
            <div class="bak-modal-footer bak-gap8">
                <button class="fm-toolbar-btn btn-green" id="bak-genkey-ok"><i class="fas fa-check"></i> Rozumiem, zapisałem klucz</button>
            </div>
        </div>
    </div>
    </div>`;

    const QS = id => body.querySelector(id);

    /* ── Sidebar navigation ── */
    body.querySelectorAll('.bak-nav-item').forEach(tab => {
        tab.onclick = () => {
            state.tab = tab.dataset.tab;
            body.querySelectorAll('.bak-nav-item').forEach(t => t.classList.remove('active'));
            body.querySelectorAll('.bak-panel').forEach(p => p.classList.remove('active'));
            tab.classList.add('active');
            QS('#bak-panel-' + state.tab).classList.add('active');
            if (state.tab === 'dashboard') renderDashboard();
        };
    });

    /* ── Dashboard quick action cards ── */
    body.querySelectorAll('.bak-dash-card[data-goto]').forEach(card => {
        card.onclick = () => {
            var target = card.dataset.goto;
            body.querySelectorAll('.bak-nav-item').forEach(t => t.classList.toggle('active', t.dataset.tab === target));
            body.querySelectorAll('.bak-panel').forEach(p => p.classList.remove('active'));
            QS('#bak-panel-' + target).classList.add('active');
            state.tab = target;
        };
    });

    function renderDashboard() {
        var sumEl = QS('#bak-dash-summary');
        if (!sumEl) return;
        var bakCount = state.backups.length;
        var profCount = state.profiles.length;
        var schedCount = state.profiles.filter(function(p) {
            var sch = typeof p.schedule === 'string' ? JSON.parse(p.schedule || 'null') : p.schedule;
            return sch && sch.type !== 'manual';
        }).length;
        var snapCount = typeof snapshots !== 'undefined' ? snapshots.length : 0;
        var sshCount = state.sshServers.length;
        var usbCount = state.usbDrives.length;
        sumEl.innerHTML =
            '<div class="bak-dash-stat"><span class="bak-dash-stat-icon"><i class="fas fa-archive"></i></span><span class="bak-dash-stat-val">' + bakCount + '</span><span class="bak-dash-stat-label">' + t('Kopie zapasowe') + '</span></div>' +
            '<div class="bak-dash-stat"><span class="bak-dash-stat-icon"><i class="fas fa-camera-retro"></i></span><span class="bak-dash-stat-val">' + snapCount + '</span><span class="bak-dash-stat-label">' + t('Punkty przywracania') + '</span></div>' +
            '<div class="bak-dash-stat"><span class="bak-dash-stat-icon"><i class="fas fa-calendar-check"></i></span><span class="bak-dash-stat-val">' + schedCount + '</span><span class="bak-dash-stat-label">' + t('Harmonogramy') + '</span></div>';

        /* ── Shield Status computation ── */
        var shieldIcon = QS('#bak-shield-icon');
        var shieldLabel = QS('#bak-shield-label');
        var shieldDetail = QS('#bak-shield-detail');
        var shieldBox = QS('#bak-shield-status');
        if (!shieldIcon) return;

        var now = Date.now();
        var lastBackupMs = 0;
        var lastBackupOk = false;
        state.history.forEach(function(h) {
            var ts = h.timestamp ? new Date(h.timestamp).getTime() : 0;
            if (ts > lastBackupMs) { lastBackupMs = ts; lastBackupOk = h.status === 'completed'; }
        });
        var lastSnapMs = 0;
        if (typeof snapshots !== 'undefined') {
            snapshots.forEach(function(s) {
                if (!s.received) {
                    var ts = s.created ? new Date(s.created).getTime() : 0;
                    if (ts > lastSnapMs) lastSnapMs = ts;
                }
            });
        }

        var backupAgeH = lastBackupMs ? (now - lastBackupMs) / 3600000 : Infinity;
        var snapAgeH = lastSnapMs ? (now - lastSnapMs) / 3600000 : Infinity;

        var level = 'unknown'; // unknown, ok, warning, error
        var details = [];

        if (bakCount === 0 && snapCount === 0 && profCount === 0) {
            level = 'unknown';
            details.push(t('Brak kopii — rozpocznij od utworzenia pierwszej kopii zapasowej'));
        } else if ((!lastBackupOk && lastBackupMs > 0) || (backupAgeH > 72 && schedCount > 0)) {
            level = 'error';
            if (!lastBackupOk && lastBackupMs > 0) details.push(t('Ostatni backup zakończył się błędem'));
            if (backupAgeH > 72 && schedCount > 0) details.push(t('Brak udanego backupu od ponad 3 dni'));
            if (snapAgeH > 168) details.push(t('Brak punktu przywracania od ponad 7 dni'));
        } else if (backupAgeH > 48 || snapAgeH > 168 || schedCount === 0) {
            level = 'warning';
            if (backupAgeH > 48) details.push(t('Ostatni backup ponad 2 dni temu'));
            if (snapAgeH > 168) details.push(t('Brak punktu przywracania od ponad 7 dni'));
            if (schedCount === 0 && profCount > 0) details.push(t('Brak aktywnych harmonogramów'));
        } else {
            level = 'ok';
        }

        var icons = {
            ok: '<i class="fas fa-shield-alt bak-text-success"></i>',
            warning: '<i class="fas fa-shield-alt bak-text-warning"></i>',
            error: '<i class="fas fa-shield-alt bak-text-danger"></i>',
            unknown: '<i class="fas fa-shield-alt bak-text-gray"></i>',
        };
        var labels = {
            ok: t('Chroniony'),
            warning: t('Wymaga uwagi'),
            error: t('Dane zagrożone'),
            unknown: t('Nieskonfigurowany'),
        };
        shieldIcon.innerHTML = icons[level];
        shieldLabel.textContent = labels[level];
        shieldBox.className = 'bak-shield-status bak-shield-' + level;

        var detailParts = [];
        if (lastBackupMs) {
            var bakDt = new Date(lastBackupMs);
            detailParts.push('<i class="fas fa-archive"></i> ' + t('Ostatni backup: ') + bakDt.toLocaleString(getLocale()));
        }
        if (lastSnapMs) {
            var snapDt = new Date(lastSnapMs);
            detailParts.push('<i class="fas fa-camera-retro"></i> ' + t('Ostatni punkt: ') + snapDt.toLocaleString(getLocale()));
        }
        if (details.length) {
            detailParts.push('<span style="color:' + (level==='error'?'#ef4444':level==='warning'?'#f59e0b':'var(--text-muted)') + '">' + details.join(' · ') + '</span>');
        }
        shieldDetail.innerHTML = detailParts.join('<br>');
    }

    /* ══════════════════════════════════════════════════════════
       PROFILES
       ══════════════════════════════════════════════════════════ */
    async function loadProfiles() {
        try {
            state.profiles = (await api('/backup/profiles')).profiles || [];
            renderProfiles();
        } catch(e) {}
    }

    function renderProfiles() {
        const el = QS('#bak-profiles-list');
        if (!state.profiles.length) {
            el.innerHTML = `<div class="bak-empty-state"><i class="fas fa-bookmark"></i><p>${t('Brak profili backupu')}</p><span>${t('Utwórz nowy profil, aby rozpocząć')}</span></div>`;
            return;
        }
        el.innerHTML = state.profiles.map(p => {
            const sched = typeof p.schedule === 'string' ? JSON.parse(p.schedule || 'null') : p.schedule;
            const dest = typeof p.destination === 'string' ? JSON.parse(p.destination || 'null') : p.destination;
            const paths = typeof p.paths === 'string' ? JSON.parse(p.paths || '[]') : (p.paths || []);

            /* Schedule label */
            var schedHtml = '';
            if (sched && sched.type === 'daily') {
                schedHtml = '<span class="bak-badge bak-badge-blue"><i class="fas fa-clock"></i> Codziennie o ' + (sched.time || '03:00') + '</span>';
            } else if (sched && sched.type === 'weekly') {
                var dayNames = ['Pn','Wt',t('Śr'),'Cz','Pt','So','Nd'];
                var days = (sched.days || []).map(function(d) { return dayNames[d] || d; }).join(', ');
                schedHtml = `<span class="bak-badge bak-badge-blue"><i class="fas fa-calendar-alt"></i> ${t('Co tydzień (')}` + days + ') o ' + (sched.time || '03:00') + '</span>';
            } else {
                schedHtml = `<span class="bak-badge"><i class="fas fa-hand-pointer"></i> ${t('Ręczny')}</span>`;
            }

            var destHtml = destLabelHtml(dest);
            var incrHtml = p.incremental ? '<span class="bak-badge bak-badge-purple"><i class="fas fa-layer-group"></i> ' + t('Tylko zmiany') + '</span>' : '<span class="bak-badge bak-badge-green"><i class="fas fa-clone"></i> ' + t('Pełna kopia') + '</span>';
            var enc = typeof p.encryption === 'string' ? JSON.parse(p.encryption || 'null') : p.encryption;
            var encHtml = '';
            if (enc && enc.enabled) {
                var encLabel = enc.mode === 'key' ? 'Klucz auto.' : 'Szyfrowany';
                var encIcon = enc.mode === 'key' ? 'fa-key' : 'fa-lock';
                encHtml = '<span class="bak-badge bak-badge-orange"><i class="fas ' + encIcon + '"></i> ' + encLabel + '</span>';
            }

            /* Paths as individual pills */
            var pathsHtml = paths.map(function(pt) {
                return '<span class="bak-path-pill" title="' + pt + '"><i class="fas fa-folder"></i> ' + prettyPath(pt) + '</span>';
            }).join('');

            /* Destination detail row */
            var destDetailHtml = '';
            if (dest && dest.type === 'usb') {
                destDetailHtml = '<i class="fas fa-usb"></i> USB: <strong>' + (dest.path || '/media/usb') + '</strong>';
            } else if (dest && dest.type === 'ssh') {
                var srv = state.sshServers.find(function(s) { return String(s.id) === String(dest.server_id); });
                if (srv) {
                    destDetailHtml = '<i class="fas fa-server"></i> SSH: <strong>' + srv.name + '</strong> (' + srv.username + '@' + srv.host + ':' + (srv.remote_path || '/backups') + ')';
                } else {
                    destDetailHtml = '<i class="fas fa-server"></i> SSH (id: ' + dest.server_id + ')';
                }
            } else {
                destDetailHtml = '<i class="fas fa-hdd"></i> Lokalnie: <strong>/app/backups</strong>';
            }

            return '<div class="bak-profile-card">'
                + '<div class="bak-profile-card-header">'
                + '<div class="bak-profile-icon"><i class="fas fa-bookmark"></i></div>'
                + '<div class="bak-profile-title">'
                + '<strong>' + p.name + '</strong>'
                + '<div class="bak-profile-badges">' + schedHtml + incrHtml + encHtml + '</div>'
                + '</div>'
                + '</div>'
                + '<div class="bak-profile-card-body">'
                + '<div class="bak-profile-row">'
                + `<span class="bak-profile-label"><i class="fas fa-folder-open"></i> ${t('Skąd:')}</span>`
                + '<div class="bak-profile-paths">' + pathsHtml + '</div>'
                + '</div>'
                + '<div class="bak-profile-row">'
                + `<span class="bak-profile-label"><i class="fas fa-download"></i> ${t('Dokąd:')}</span>`
                + '<div class="bak-profile-dest-detail">' + destDetailHtml + '</div>'
                + '</div>'
                + '<div class="bak-profile-row">'
                + '<span class="bak-profile-detail"><i class="fas fa-archive"></i> ' + t('Zachowaj kopii:') + ' <strong>' + (p.retention || '∞') + '</strong></span>'
                + '</div>'
                + '</div>'
                + '<div class="bak-profile-card-actions">'
                + '<button class="fm-toolbar-btn btn-green btn-sm" data-run-profile="' + p.id + '"><i class="fas fa-play"></i> Uruchom</button>'
                + '<button class="fm-toolbar-btn btn-sm" data-edit-profile="' + p.id + '"><i class="fas fa-edit"></i> Edytuj</button>'
                + (enc && enc.enabled && enc.mode === 'key' ? '<button class="fm-toolbar-btn btn-sm" data-view-key="' + p.id + '" title="Pokaż klucz szyfrowania"><i class="fas fa-key"></i></button>' : '')
                + '<button class="fm-toolbar-btn btn-red btn-sm" data-del-profile="' + p.id + '"><i class="fas fa-trash"></i></button>'
                + '</div></div>';
        }).join('');

        el.querySelectorAll('button[data-run-profile]').forEach(btn => {
            btn.onclick = async () => {
                if (state.busy) { toast('Operacja w toku', 'warning'); return; }
                var profileId = btn.dataset.runProfile;
                var profile = state.profiles.find(function(p) { return String(p.id) === String(profileId); });
                var enc = profile && (typeof profile.encryption === 'string' ? JSON.parse(profile.encryption || 'null') : profile.encryption);
                var body = {};
                if (enc && enc.enabled && enc.mode !== 'key') {
                    // Passphrase mode: ask user; key mode: backend resolves automatically
                    var pw = await promptPassphrase('Hasło szyfrowania backupu', true);
                    if (!pw) return;
                    body.encrypt_passphrase = pw;
                }
                try {
                    await api('/backup/profiles/' + profileId + '/run', { method: 'POST', body: body });
                    state.busy = true;
                    switchTab('backup');
                    showProgress();
                    toast(t('Backup z profilu rozpoczęty'), 'info');
                } catch(e) { toast(t('Błąd uruchomienia'), 'error'); }
            };
        });
        el.querySelectorAll('button[data-edit-profile]').forEach(btn => {
            btn.onclick = () => openProfileEditor(state.profiles.find(p => String(p.id) === String(btn.dataset.editProfile)));
        });
        el.querySelectorAll('button[data-view-key]').forEach(btn => {
            btn.onclick = async () => {
                try {
                    var resp = await api('/backup/profiles/' + btn.dataset.viewKey + '/key');
                    if (resp && resp.key) showGeneratedKey(resp.key);
                } catch(e) { toast('Błąd pobierania klucza', 'error'); }
            };
        });
        el.querySelectorAll('button[data-del-profile]').forEach(btn => {
            btn.onclick = async () => {
                if (!confirm(t('Usunąć profil?'))) return;
                await api('/backup/profiles/' + btn.dataset.delProfile, { method: 'DELETE' });
                loadProfiles();
            };
        });
    }

    /* Scheduled backups list */
    async function loadScheduled() {
        try {
            const data = await api('/backup/scheduled-backups');
            const el = QS('#bak-scheduled-list');
            const sch = data.scheduled || [];
            if (!sch.length) { el.innerHTML = `<p class="bak-text-muted">${t('Brak zaplanowanych backupów.')}</p>`; return; }
            el.innerHTML = sch.map(s => {
                return '<div class="bak-scheduled-item">'
                    + '<i class="fas fa-calendar-alt bak-icon-accent"></i> '
                    + '<strong>' + s.profile_name + '</strong> '
                    + '<span class="bak-badge bak-badge-blue">' + s.schedule.type + (s.schedule.time ? ' @ '+s.schedule.time : '') + '</span>'
                    + (s.last_run ? '<span class="bak-caption-sm">Ostatni: ' + new Date(s.last_run).toLocaleString('pl') + '</span>' : '')
                    + (s.next_run ? `<span class="bak-caption-accent">${t('Następny:')} ` + new Date(s.next_run).toLocaleString('pl') + '</span>' : '')
                    + '</div>';
            }).join('');
        } catch(e) {}
    }

    /* ── Profile editor modal ── */
    var pmEditId = null;
    var pmPaths = [];

    function openProfileEditor(profile) {
        pmEditId = profile ? profile.id : null;
        var title = profile ? 'Edytuj profil' : 'Nowy profil';
        QS('#bak-pm-title').innerHTML = '<i class="fas fa-bookmark"></i> ' + title;

        if (profile) {
            var paths = typeof profile.paths === 'string' ? JSON.parse(profile.paths) : (profile.paths || []);
            var dest = typeof profile.destination === 'string' ? JSON.parse(profile.destination || 'null') : profile.destination;
            var sched = typeof profile.schedule === 'string' ? JSON.parse(profile.schedule || 'null') : profile.schedule;

            QS('#bak-pm-name').value = profile.name || '';
            pmPaths = paths.slice();
            QS('#bak-pm-retention').value = profile.retention || 0;
            QS('#bak-pm-incr').checked = !!profile.incremental;

            // Destination
            if (dest) {
                QS('#bak-pm-dest').value = dest.type || 'local';
                updatePmDest();
                if (dest.type === 'usb' && dest.path) {
                    var usbSel = QS('#bak-pm-dest-usb');
                    /* Check if the path matches a USB mount point exactly */
                    var matchesDrive = false;
                    for (var i = 0; i < usbSel.options.length; i++) {
                        if (usbSel.options[i].value === dest.path) { matchesDrive = true; break; }
                    }
                    if (matchesDrive) {
                        usbSel.value = dest.path;
                        pmDestCustomPath = null;
                        QS('#bak-pm-dest-path-display').classList.add('hidden');
                    } else {
                        /* It's a subfolder — select the base USB drive and show custom path */
                        var bestDrive = '';
                        for (var j = 0; j < usbSel.options.length; j++) {
                            if (dest.path.startsWith(usbSel.options[j].value)) {
                                if (usbSel.options[j].value.length > bestDrive.length) bestDrive = usbSel.options[j].value;
                            }
                        }
                        if (bestDrive) usbSel.value = bestDrive;
                        else {
                            usbSel.innerHTML += '<option value="' + dest.path + '">' + dest.path.split('/').pop() + '</option>';
                            usbSel.value = dest.path;
                        }
                        pmDestCustomPath = dest.path;
                        QS('#bak-pm-dest-path-display').classList.remove('hidden');
                        QS('#bak-pm-dest-path-text').textContent = prettyPath(dest.path);
                    }
                }
                if (dest.type === 'ssh' && dest.server_id) {
                    QS('#bak-pm-dest-ssh').value = dest.server_id;
                }
            } else {
                QS('#bak-pm-dest').value = 'local';
                pmDestCustomPath = null;
                QS('#bak-pm-dest-path-display').classList.add('hidden');
                updatePmDest();
            }

            // Schedule
            if (sched) {
                QS('#bak-pm-sched-type').value = sched.type || 'manual';
                QS('#bak-pm-sched-time').value = sched.time || '03:00';
                updatePmSchedType();
                if (sched.type === 'weekly' && sched.days) {
                    body.querySelectorAll('#bak-pm-days input[type=checkbox]').forEach(function(cb) {
                        cb.checked = sched.days.indexOf(parseInt(cb.value)) !== -1;
                    });
                }
            } else {
                QS('#bak-pm-sched-type').value = 'manual';
                QS('#bak-pm-sched-time').value = '03:00';
                updatePmSchedType();
            }

            // Encryption
            var enc = typeof profile.encryption === 'string' ? JSON.parse(profile.encryption || 'null') : profile.encryption;
            var encEnabled = enc && enc.enabled;
            var encMode = (enc && enc.mode) || 'passphrase';
            QS('#bak-pm-encrypt').checked = !!encEnabled;
            QS('#bak-pm-encrypt-opts').classList.toggle('hidden', !encEnabled);
            QS('#bak-pm-enc-passphrase').checked = encMode !== 'key';
            QS('#bak-pm-enc-key').checked = encMode === 'key';
            updatePmEncMode();
        } else {
            QS('#bak-pm-name').value = '';
            pmPaths = state.paths.slice();
            QS('#bak-pm-retention').value = 3;
            QS('#bak-pm-incr').checked = false;
            QS('#bak-pm-encrypt').checked = false;
            QS('#bak-pm-encrypt-opts').classList.add('hidden');
            QS('#bak-pm-enc-passphrase').checked = true;
            QS('#bak-pm-enc-key').checked = false;
            updatePmEncMode();
            QS('#bak-pm-dest').value = 'local';
            QS('#bak-pm-sched-type').value = 'manual';
            QS('#bak-pm-sched-time').value = '03:00';
            pmDestCustomPath = null;
            QS('#bak-pm-dest-path-display').classList.add('hidden');
            updatePmDest();
            updatePmSchedType();
        }
        renderPmPaths();
        QS('#bak-profile-modal').classList.remove('hidden');
    }

    function renderPmPaths() {
        var el = QS('#bak-pm-paths');
        if (pmPaths.length) {
            el.innerHTML = pmPaths.map(function(p, i) {
                return '<div class="bak-path-item">'
                    + '<i class="fas fa-folder bak-icon-accent"></i> '
                    + '<div class="bak-path-info">'
                    + '<div class="bak-path-pretty">' + prettyPath(p) + '</div>'
                    + '<div class="bak-path-raw">' + p + '</div>'
                    + '</div>'
                    + '<button class="fm-toolbar-btn btn-red btn-sm" data-pm-rm="' + i + '"><i class="fas fa-times"></i></button>'
                    + '</div>';
            }).join('');
        } else {
            el.innerHTML = `<p class="bak-hint">${t('Dodaj ścieżki do backupu')}</p>`;
        }
        el.querySelectorAll('[data-pm-rm]').forEach(function(btn) {
            btn.onclick = function() { pmPaths.splice(parseInt(btn.dataset.pmRm), 1); renderPmPaths(); };
        });
    }

    // Profile browse — handled below in openDirPicker section

    function updatePmDest() {
        var t = QS('#bak-pm-dest').value;
        QS('#bak-pm-dest-usb').classList.toggle('hidden', t !== 'usb');
        QS('#bak-pm-dest-usb-browse').classList.toggle('hidden', t !== 'usb');
        QS('#bak-pm-dest-ssh').classList.toggle('hidden', t !== 'ssh');
        if (t !== 'usb') {
            pmDestCustomPath = null;
            QS('#bak-pm-dest-path-display').classList.add('hidden');
        }
    }
    QS('#bak-pm-dest').onchange = updatePmDest;

    function updatePmSchedType() {
        var t = QS('#bak-pm-sched-type').value;
        QS('#bak-pm-days').classList.toggle('hidden', t !== 'weekly');
    }
    QS('#bak-pm-sched-type').onchange = updatePmSchedType;

    QS('#bak-pm-encrypt').onchange = function() {
        var checked = this.checked;
        QS('#bak-pm-encrypt-opts').classList.toggle('hidden', !checked);
        if (!checked) {
            QS('#bak-pm-enc-passphrase').checked = true;
            updatePmEncMode();
        }
    };

    function updatePmEncMode() {
        var mode = QS('#bak-pm-enc-passphrase').checked ? 'passphrase' : 'key';
        QS('#bak-pm-encrypt-warning').classList.toggle('hidden', mode !== 'passphrase');
        QS('#bak-pm-key-info').classList.toggle('hidden', mode !== 'key');
    }
    QS('#bak-pm-enc-passphrase').onchange = updatePmEncMode;
    QS('#bak-pm-enc-key').onchange = updatePmEncMode;

    QS('#bak-pm-close').onclick = function() { QS('#bak-profile-modal').classList.add('hidden'); };
    QS('#bak-pm-cancel').onclick = function() { QS('#bak-profile-modal').classList.add('hidden'); };

    QS('#bak-pm-save').onclick = async function() {
        var name = QS('#bak-pm-name').value.trim();
        if (!name) { toast(t('Podaj nazwę'), 'warning'); return; }
        if (!pmPaths.length) { toast(t('Dodaj ścieżki'), 'warning'); return; }

        var dest = null;
        var dt = QS('#bak-pm-dest').value;
        if (dt === 'usb') dest = { type: 'usb', path: pmDestCustomPath || QS('#bak-pm-dest-usb').value };
        else if (dt === 'ssh') dest = { type: 'ssh', server_id: QS('#bak-pm-dest-ssh').value };

        var schedType = QS('#bak-pm-sched-type').value;
        var schedule = null;
        if (schedType !== 'manual') {
            schedule = { type: schedType, time: QS('#bak-pm-sched-time').value };
            if (schedType === 'weekly') {
                schedule.days = [];
                body.querySelectorAll('#bak-pm-days input:checked').forEach(function(cb) { schedule.days.push(parseInt(cb.value)); });
            }
        }

        var encEnabled = QS('#bak-pm-encrypt').checked;
        var encMode = encEnabled ? (QS('#bak-pm-enc-key').checked ? 'key' : 'passphrase') : null;
        var payload = {
            name: name, paths: pmPaths, destination: dest, schedule: schedule,
            retention: parseInt(QS('#bak-pm-retention').value) || 0,
            incremental: QS('#bak-pm-incr').checked,
            encryption: encEnabled ? { enabled: true, mode: encMode } : null,
        };
        try {
            var resp;
            if (pmEditId) {
                resp = await api('/backup/profiles/' + pmEditId, { method: 'PUT', body: payload });
            } else {
                resp = await api('/backup/profiles', { method: 'POST', body: payload });
            }
            toast('Profil zapisany', 'success');
            QS('#bak-profile-modal').classList.add('hidden');
            loadProfiles();
            loadScheduled();
            if (resp && resp.generated_key) {
                showGeneratedKey(resp.generated_key);
            }
        } catch(e) { toast(t('Błąd zapisu'), 'error'); }
    };

    QS('#bak-profile-new').onclick = function() { openProfileEditor(null); };

    /* ── Profile Export ── */
    QS('#bak-profile-export').onclick = async function() {
        try {
            var data = await api('/backup/profiles/export');
            var blob = new Blob([JSON.stringify(data, null, 2)], { type: 'application/json' });
            var url = URL.createObjectURL(blob);
            var a = document.createElement('a');
            a.href = url;
            a.download = 'backup-profiles-' + new Date().toISOString().slice(0,10) + '.json';
            document.body.appendChild(a);
            a.click();
            document.body.removeChild(a);
            URL.revokeObjectURL(url);
            toast('Profile wyeksportowane', 'success');
        } catch(e) { toast(t('Błąd eksportu'), 'error'); }
    };

    /* ── Profile Import ── */
    QS('#bak-profile-import').onclick = function() {
        QS('#bak-profile-import-file').click();
    };

    QS('#bak-profile-import-file').onchange = async function(e) {
        var file = e.target.files[0];
        if (!file) return;
        try {
            var formData = new FormData();
            formData.append('file', file);
            var resp = await fetch('/api/backup/profiles/import', {
                method: 'POST',
                body: formData,
                credentials: 'same-origin'
            });
            var result = await resp.json();
            if (result.success) {
                toast(result.message || 'Profile zaimportowane', 'success');
                loadProfiles();
                loadScheduled();
            } else {
                toast(result.error || t('Błąd importu'), 'error');
            }
        } catch(e) { toast(t('Błąd importu pliku'), 'error'); }
        // Reset file input
        QS('#bak-profile-import-file').value = '';
    };

    /* ══════════════════════════════════════════════════════════
       PASSPHRASE MODAL — for encrypted backup/restore
       ══════════════════════════════════════════════════════════ */
    var _passphraseResolve = null;

    function promptPassphrase(titleText, needConfirm) {
        return new Promise(function(resolve) {
            _passphraseResolve = resolve;
            QS('#bak-passphrase-title').innerHTML = '<i class="fas fa-lock"></i> ' + (titleText || 'Hasło szyfrowania');
            QS('#bak-passphrase-input').value = '';
            QS('#bak-passphrase-confirm').value = '';
            QS('#bak-passphrase-confirm-row').classList.toggle('hidden', !needConfirm);
            QS('#bak-passphrase-modal').classList.remove('hidden');
            QS('#bak-passphrase-input').focus();
        });
    }

    QS('#bak-passphrase-close').onclick = function() {
        QS('#bak-passphrase-modal').classList.add('hidden');
        if (_passphraseResolve) { _passphraseResolve(null); _passphraseResolve = null; }
    };
    QS('#bak-passphrase-cancel').onclick = function() {
        QS('#bak-passphrase-modal').classList.add('hidden');
        if (_passphraseResolve) { _passphraseResolve(null); _passphraseResolve = null; }
    };
    QS('#bak-passphrase-ok').onclick = function() {
        var pw = QS('#bak-passphrase-input').value;
        var confirmRow = QS('#bak-passphrase-confirm-row');
        if (!pw) { toast(t('Podaj hasło'), 'warning'); return; }
        if (!confirmRow.classList.contains('hidden')) {
            var pw2 = QS('#bak-passphrase-confirm').value;
            if (pw !== pw2) { toast('Hasła nie są zgodne', 'error'); return; }
        }
        QS('#bak-passphrase-modal').classList.add('hidden');
        if (_passphraseResolve) { _passphraseResolve(pw); _passphraseResolve = null; }
    };
    QS('#bak-passphrase-input').onkeydown = function(e) {
        if (e.key === 'Enter') QS('#bak-passphrase-ok').click();
    };
    QS('#bak-passphrase-confirm').onkeydown = function(e) {
        if (e.key === 'Enter') QS('#bak-passphrase-ok').click();
    };

    /* ── Generated key display modal ── */
    function showGeneratedKey(key) {
        QS('#bak-genkey-value').textContent = key;
        QS('#bak-genkey-modal').classList.remove('hidden');
    }
    QS('#bak-genkey-ok').onclick = function() {
        QS('#bak-genkey-modal').classList.add('hidden');
        QS('#bak-genkey-value').textContent = '';
    };
    QS('#bak-genkey-copy').onclick = function() {
        var key = QS('#bak-genkey-value').textContent;
        if (key && navigator.clipboard) {
            navigator.clipboard.writeText(key).then(function() { toast('Klucz skopiowany', 'success'); }).catch(function() {});
        }
    };
    function renderPaths() {
        var el = QS('#bak-paths');
        if (state.paths.length) {
            el.innerHTML = state.paths.map(function(p) {
                return '<div class="bak-path-item">'
                    + '<i class="fas fa-folder bak-icon-accent"></i>'
                    + '<div class="bak-path-info">'
                    + '<div class="bak-path-pretty">' + prettyPath(p) + '</div>'
                    + '<div class="bak-path-raw">' + p + '</div>'
                    + '</div>'
                    + '<button class="fm-toolbar-btn btn-red btn-sm" data-rm-path="' + p + '"><i class="fas fa-times"></i></button>'
                    + '</div>';
            }).join('');
        } else {
            el.innerHTML = `<div class="bak-empty-state bak-empty-sm"><i class="fas fa-folder-plus"></i><p>${t('Nie wybrano ścieżek')}</p><span>${t('Dodaj ścieżki do backupu poniżej')}</span></div>`;
        }
        el.querySelectorAll('button[data-rm-path]').forEach(function(btn) {
            btn.onclick = async function() {
                try { await api('/backup/paths', { method: 'DELETE', body: { path: btn.dataset.rmPath } }); loadPaths(); } catch(e) { toast(t('Błąd'), 'error'); }
            };
        });
    }

    async function loadPaths() {
        try { state.paths = (await api('/backup/paths')).paths || []; renderPaths(); } catch(e) {}
    }

    /* ── Lightweight folder picker (like DL Manager) ── */
    var pmDestCustomPath = null;

    function openDirPicker(startPath, title, onSelect, opts) {
        opts = opts || {};
        var browsePath = startPath || null; // null = virtual root (list of BROWSE_ROOTS)
        var overlay = document.createElement('div');
        overlay.className = 'modal-overlay';
        overlay.innerHTML = '<div class="modal-box bak-modal-480">'
            + '<div class="modal-header"><span>' + (title || 'Wybierz folder') + '</span><button class="modal-close"><i class="fas fa-times"></i></button></div>'
            + '<div class="modal-body bak-p0">'
            + '<div class="bak-modal-bar">'
            + '<button class="fm-toolbar-btn btn-sm" id="bak-pick-up"><i class="fas fa-arrow-up"></i></button>'
            + '<span id="bak-pick-path" class="bak-item-name"></span>'
            + '</div>'
            + '<div id="bak-pick-list" class="bak-scroll-280"></div>'
            + '</div>'
            + '<div class="modal-footer">'
            + '<button class="btn btn-secondary" id="bak-pick-cancel">Anuluj</button>'
            + '<button class="btn btn-primary" id="bak-pick-select"><i class="fas fa-check"></i> Wybierz</button>'
            + '</div></div>';
        document.body.appendChild(overlay);

        var pathEl = overlay.querySelector('#bak-pick-path');
        var listEl = overlay.querySelector('#bak-pick-list');
        var selectBtn = overlay.querySelector('#bak-pick-select');
        var close = function() { overlay.remove(); };

        overlay.querySelector('.modal-close').addEventListener('click', close);
        overlay.querySelector('#bak-pick-cancel').addEventListener('click', close);
        overlay.addEventListener('click', function(e) { if (e.target === overlay) close(); });
        overlay.querySelector('#bak-pick-up').addEventListener('click', function() {
            if (!browsePath) return;
            var parent = browsePath.substring(0, browsePath.lastIndexOf('/')) || null;
            // go to virtual root if parent is not a valid sub-path
            if (!parent || parent === '') { loadRoots(); return; }
            loadDir(parent);
        });
        selectBtn.addEventListener('click', function() {
            if (!browsePath) { return; } // can't select virtual root
            onSelect(browsePath);
            close();
        });

        function loadRoots() {
            browsePath = null;
            pathEl.textContent = '/';
            selectBtn.disabled = true;
            listEl.innerHTML = '<div class="bak-empty-xs"><i class="fas fa-spinner fa-spin"></i></div>';
            api('/backup/browse/roots').then(function(data) {
                listEl.innerHTML = '';
                (data.roots || []).forEach(function(r) {
                    var row = document.createElement('div');
                    row.className = 'dte-browse-item';
                    var freeLabel = r.free != null ? ' <span class="bak-hint">(' + (r.free/1073741824).toFixed(0) + ' GB wolne)</span>' : '';
                    row.innerHTML = '<i class="fas fa-hdd bak-icon-accent"></i><span class="bak-flex1-ellipsis"> ' + r.name + freeLabel + '</span>';
                    row.addEventListener('click', function() { loadDir(r.path); });
                    listEl.appendChild(row);
                });
                if (!data.roots || !data.roots.length) {
                    listEl.innerHTML = `<div class="bak-empty-xs">${t('Brak dostępnych folderów')}</div>`;
                }
            }).catch(function() {
                listEl.innerHTML = `<div class="bak-empty-error"><i class="fas fa-exclamation-triangle"></i> ${t('Brak dostępu')}</div>`;
            });
        }

        function loadDir(path) {
            browsePath = path;
            pathEl.textContent = prettyPath(path);
            selectBtn.disabled = false;
            listEl.innerHTML = '<div class="bak-empty-xs"><i class="fas fa-spinner fa-spin"></i></div>';
            var apiUrl = opts.usb ? '/backup/usb-browse?path=' : '/backup/browse?path=';
            api(apiUrl + encodeURIComponent(path)).then(function(data) {
                var dirs = (data.items || []).filter(function(i) { return i.isDir; }).sort(function(a, b) { return a.name.localeCompare(b.name); });
                listEl.innerHTML = '';
                if (!dirs.length) {
                    listEl.innerHTML = `<div class="bak-empty-xs"><i class="fas fa-folder-open"></i> ${t('Brak podfolderów')}</div>`;
                }
                dirs.forEach(function(d) {
                    var row = document.createElement('div');
                    row.className = 'dte-browse-item';
                    row.innerHTML = '<i class="fas fa-folder bak-text-warning"></i><span class="bak-flex1-ellipsis"> ' + d.name + '</span>';
                    row.addEventListener('click', function() { loadDir(d.path); });
                    listEl.appendChild(row);
                });
            }).catch(function() {
                listEl.innerHTML = `<div class="bak-empty-error"><i class="fas fa-exclamation-triangle"></i> ${t('Brak dostępu')}</div>`;
            });
        }

        if (opts.usb || browsePath) {
            loadDir(browsePath);
        } else {
            loadRoots();
        }
    }

    /* ── Ad-hoc source browse ── */
    QS('#bak-browse-btn').onclick = function() {
        openDirPicker(null, 'Dodaj folder do backupu', async function(path) {
            try {
                await api('/backup/paths', { method: 'POST', body: { path: path } });
                loadPaths();
                toast('Dodano', 'success');
            } catch(e) { toast(t('Błąd'), 'error'); }
        });
    };

    /* ── Profile source browse ── */
    QS('#bak-pm-browse').onclick = function() {
        openDirPicker(null, 'Dodaj folder do profilu', function(path) {
            if (pmPaths.indexOf(path) === -1) { pmPaths.push(path); renderPmPaths(); }
        });
    };

    /* ── USB destination browse (ad hoc) ── */
    QS('#bak-dest-usb-browse').onclick = function() {
        var base = QS('#bak-dest-usb').value;
        if (!base) { toast('Wybierz dysk USB', 'warning'); return; }
        openDirPicker(base, 'Wybierz folder na USB', function(path) {
            adHocDestCustomPath = path;
            QS('#bak-dest-path-display').classList.remove('hidden');
            QS('#bak-dest-path-text').textContent = prettyPath(path);
        }, { usb: true });
    };

    /* ── USB destination browse (profile) ── */
    QS('#bak-pm-dest-usb-browse').onclick = function() {
        var base = QS('#bak-pm-dest-usb').value;
        if (!base) { toast('Wybierz dysk USB', 'warning'); return; }
        openDirPicker(base, 'Wybierz folder na USB', function(path) {
            pmDestCustomPath = path;
            QS('#bak-pm-dest-path-display').classList.remove('hidden');
            QS('#bak-pm-dest-path-text').textContent = prettyPath(path);
        }, { usb: true });
    };

    /* ── Dest path clear buttons ── */
    QS('#bak-dest-path-clear').onclick = function() {
        adHocDestCustomPath = null;
        QS('#bak-dest-path-display').classList.add('hidden');
    };
    QS('#bak-pm-dest-path-clear').onclick = function() {
        pmDestCustomPath = null;
        QS('#bak-pm-dest-path-display').classList.add('hidden');
    };

    /* ── Destination selects (ad hoc) ── */
    var adHocDestCustomPath = null;

    function updateDestSelects() {
        var type = QS('#bak-dest-type').value;
        QS('#bak-dest-usb').classList.toggle('hidden', type !== 'usb');
        QS('#bak-dest-usb-browse').classList.toggle('hidden', type !== 'usb');
        QS('#bak-dest-ssh').classList.toggle('hidden', type !== 'ssh');
        if (type !== 'usb') {
            adHocDestCustomPath = null;
            QS('#bak-dest-path-display').classList.add('hidden');
        }
    }
    QS('#bak-dest-type').onchange = updateDestSelects;

    /* ── USB ── */
    async function loadUSB() {
        try {
            state.usbDrives = (await api('/backup/usb-drives')).drives || [];
            var opts = state.usbDrives.map(function(d) {
                return '<option value="' + d.path + '">' + d.name + ' (' + (d.free/1073741824).toFixed(1) + ' GB wolne)</option>';
            }).join('') || '<option value="">Brak USB</option>';
            QS('#bak-dest-usb').innerHTML = opts;
            QS('#bak-pm-dest-usb').innerHTML = opts;
            renderUSBList();
        } catch(e) {}
    }
    function renderUSBList() {
        var el = QS('#bak-usb-list');
        if (state.usbDrives.length) {
            el.innerHTML = state.usbDrives.map(function(d) {
                return '<div class="bak-usb-item">'
                    + '<i class="fas fa-usb bak-icon-accent"></i> '
                    + '<span><strong>' + d.name + '</strong></span> '
                    + '<span>' + (d.used/1073741824).toFixed(1) + ' / ' + (d.total/1073741824).toFixed(1) + ' GB</span> '
                    + '<div class="res-bar bak-w120"><div class="res-bar-fill" style="width:' + d.percent_used + '%;background:' + (d.percent_used>90?'#ef4444':'#10b981') + '"></div></div> '
                    + '<span>' + d.percent_used + '%</span>'
                    + '</div>';
            }).join('');
        } else {
            el.innerHTML = `<p class="bak-text-muted">${t('Brak podłączonych urządzeń USB')}</p>`;
        }
    }
    QS('#bak-usb-refresh').onclick = loadUSB;

    /* ── SSH ── */
    async function loadSSH() {
        try {
            state.sshServers = (await api('/backup/ssh-servers')).servers || [];
            var opts = state.sshServers.map(function(s) {
                return '<option value="' + s.id + '">' + s.name + ' (' + s.host + ')</option>';
            }).join('') || `<option value="">${t('Brak serwerów')}</option>`;
            QS('#bak-dest-ssh').innerHTML = opts;
            QS('#bak-pm-dest-ssh').innerHTML = opts;
            renderSSHList();
        } catch(e) {}
    }
    function renderSSHList() {
        var el = QS('#bak-ssh-list');
        if (state.sshServers.length) {
            el.innerHTML = state.sshServers.map(function(s) {
                var authBadge = s.key_path
                    ? '<span class="bak-badge bak-badge-purple"><i class="fas fa-key"></i> Klucz</span>'
                    : (s.has_password ? `<span class="bak-badge bak-fs10"><i class="fas fa-lock"></i> ${t('Hasło')}</span>` : '<span class="bak-badge bak-badge-red"><i class="fas fa-exclamation-triangle"></i> Brak</span>');
                return '<div class="bak-ssh-item">'
                    + '<i class="fas fa-server bak-icon-accent"></i> '
                    + '<span><strong>' + s.name + '</strong> (' + s.host + ':' + s.port + ')</span> '
                    + '<span>' + s.username + '@' + (s.remote_path || '/backups') + '</span> '
                    + authBadge + ' '
                    + '<button class="fm-toolbar-btn btn-red btn-sm" data-del-ssh="' + s.id + '"><i class="fas fa-trash"></i></button>'
                    + '</div>';
            }).join('');
        } else {
            el.innerHTML = `<p class="bak-text-muted">${t('Brak serwerów SSH')}</p>`;
        }
        el.querySelectorAll('button[data-del-ssh]').forEach(function(btn) {
            btn.onclick = async function() {
                if (!confirm(t('Usunąć serwer?'))) return;
                await api('/backup/ssh-servers/' + btn.dataset.delSsh, { method: 'DELETE' });
                loadSSH();
            };
        });
    }
    QS('#bak-ssh-add').onclick = function() {
        QS('#bak-ssh-form').classList.remove('hidden');
        // Populate key dropdown
        api('/api/ssh/keys').then(function(r) {
            var sel = QS('#bak-ssh-key-select');
            sel.innerHTML = '<option value="">-- wybierz klucz --</option>';
            (r.keys || []).forEach(function(k) {
                sel.innerHTML += '<option value="' + k.private_path + '">' + k.name + ' (' + k.type + ')</option>';
            });
        }).catch(function(){});
    };
    QS('#bak-ssh-cancel').onclick = function() { QS('#bak-ssh-form').classList.add('hidden'); };

    // Auth type toggle
    QS('#bak-ssh-auth-type').onchange = function() {
        var isKey = this.value === 'key';
        QS('#bak-ssh-auth-pw').classList.toggle('hidden', isKey);
        QS('#bak-ssh-auth-key').classList.toggle('hidden', !isKey);
    };

    QS('#bak-ssh-test').onclick = async function() {
        var authType = QS('#bak-ssh-auth-type').value;
        var body = {
            host: QS('#bak-ssh-host').value, port: QS('#bak-ssh-port').value,
            username: QS('#bak-ssh-user').value
        };
        if (authType === 'key') {
            body.key_path = QS('#bak-ssh-key-select').value;
        } else {
            body.password = QS('#bak-ssh-pw').value;
        }
        try {
            var r = await api('/backup/ssh-servers/test', { method: 'POST', body: body });
            toast(r.success ? t('Połączenie OK') : t('Błąd: ') + (r.error || ''), r.success ? 'success' : 'error');
        } catch(e) { toast(t('Błąd testu'), 'error'); }
    };
    QS('#bak-ssh-save').onclick = async function() {
        var authType = QS('#bak-ssh-auth-type').value;
        var body = {
            name: QS('#bak-ssh-name').value, host: QS('#bak-ssh-host').value,
            port: QS('#bak-ssh-port').value, username: QS('#bak-ssh-user').value,
            remote_path: QS('#bak-ssh-path').value
        };
        if (authType === 'key') {
            body.key_path = QS('#bak-ssh-key-select').value;
            body.password = '';
        } else {
            body.password = QS('#bak-ssh-pw').value;
            body.key_path = '';
        }
        try {
            await api('/backup/ssh-servers', { method: 'POST', body: body });
            toast('Dodano', 'success');
            QS('#bak-ssh-form').classList.add('hidden');
            loadSSH();
        } catch(e) { toast(t('Błąd'), 'error'); }
    };

    /* ══════════════════════════════════════════════════════════
       START BACKUP (ad hoc)
       ══════════════════════════════════════════════════════════ */
    QS('#bak-start-btn').onclick = async function() {
        if (state.busy) { toast('Operacja w toku', 'warning'); return; }
        if (!state.paths.length) { toast(t('Dodaj ścieżki'), 'warning'); return; }
        var destination = null;
        var destType = QS('#bak-dest-type').value;
        if (destType === 'usb') {
            var usbPath = adHocDestCustomPath || QS('#bak-dest-usb').value;
            if (!usbPath) { toast('Wybierz USB', 'warning'); return; }
            destination = { type: 'usb', path: usbPath };
        } else if (destType === 'ssh') {
            var sshId = QS('#bak-dest-ssh').value;
            if (!sshId) { toast('Wybierz serwer SSH', 'warning'); return; }
            destination = { type: 'ssh', server_id: sshId };
        }
        try {
            var bodyData = { paths: state.paths, destination: destination };
            var retVal = parseInt(QS('#bak-adhoc-retention').value) || 0;
            if (retVal > 0) bodyData.retention = retVal;
            if (QS('#bak-adhoc-incr').checked) bodyData.incremental = true;
            await api('/backup/backup', { method: 'POST', body: bodyData });
            state.busy = true;
            showProgress();
            toast(t('Backup rozpoczęty'), 'info');
        } catch(e) { toast(t('Błąd startu'), 'error'); }
    };

    /* ══════════════════════════════════════════════════════════
       BACKUPS LIST + PREVIEW + RESTORE
       ══════════════════════════════════════════════════════════ */
    async function loadBackups() {
        try { state.backups = (await api('/backup/backups')).backups || []; renderBackups(); } catch(e) {}
    }

    function renderBackups() {
        var el = QS('#bak-backups-list');
        if (!state.backups.length) {
            el.innerHTML = `<div class="bak-empty-state"><i class="fas fa-archive"></i><p>${t('Brak kopii zapasowych')}</p><span>${t('Utwórz backup, aby zobaczyć kopie')}</span></div>`;
            return;
        }

        /* Group backups by profile */
        var groups = {};
        var order = [];
        state.backups.forEach(function(b) {
            var key = b.profile_name || '__none__';
            if (!groups[key]) {
                groups[key] = [];
                order.push(key);
            }
            groups[key].push(b);
        });

        var html = '';
        order.forEach(function(key) {
            var list = groups[key];
            var label = key === '__none__' ? 'Inne kopie' : key;
            var icon = key === '__none__' ? 'fa-archive' : 'fa-bookmark';
            var count = list.length;
            var totalSize = list.reduce(function(s, b) { return s + (b.size || 0); }, 0);

            html += '<div class="bak-group">'
                + '<div class="bak-group-header" data-bak-toggle="' + key + '">'
                + '<div class="bak-group-header-left">'
                + '<i class="fas fa-chevron-down bak-group-chevron"></i>'
                + '<i class="fas ' + icon + ' bak-icon-accent"></i>'
                + '<strong>' + label + '</strong>'
                + '</div>'
                + '<div class="bak-group-header-right">'
                + '<span class="bak-badge">' + count + ' ' + (count === 1 ? 'kopia' : count < 5 ? 'kopie' : 'kopii') + '</span>'
                + '<span class="bak-badge">' + formatSize(totalSize) + '</span>'
                + '</div>'
                + '</div>'
                + '<div class="bak-group-body">';

            list.forEach(function(b) {
                var isIncr = b.name.indexOf('_incr') !== -1;
                var isEnc = b.encrypted || b.name.endsWith('.gpg');
                var dt = parseBackupDate(b.name);
                var dateStr = dt ? dt.toLocaleString('pl') : new Date(b.modified).toLocaleString('pl');
                var locHtml = b.location ? '<span class="bak-badge' + (b.location.startsWith('USB') ? ' bak-badge-blue' : '') + '"><i class="fas ' + (b.location.startsWith('USB') ? 'fa-usb' : 'fa-hdd') + '"></i> ' + b.location + '</span>' : '';
                var encBadge = isEnc ? '<span class="bak-badge bak-badge-orange"><i class="fas fa-lock"></i> Szyfrowany</span>' : '';
                html += '<div class="bak-backup-card">'
                    + '<div class="bak-backup-card-left">'
                    + '<div class="bak-backup-icon ' + (isIncr ? 'bak-icon-purple' : (isEnc ? 'bak-icon-orange' : 'bak-icon-green')) + '"><i class="fas fa-' + (isEnc ? 'lock' : 'archive') + '"></i></div>'
                    + '<div class="bak-backup-info">'
                    + '<strong class="bak-backup-name">' + b.name + '</strong>'
                    + '<div class="bak-backup-meta">'
                    + '<span><i class="fas fa-calendar"></i> ' + dateStr + '</span>'
                    + '<span><i class="fas fa-weight-hanging"></i> ' + formatSize(b.size) + '</span>'
                    + (isIncr ? '<span class="bak-badge bak-badge-purple">' + t('Tylko zmiany') + '</span>' : '<span class="bak-badge bak-badge-green">' + t('Pełna kopia') + '</span>')
                    + encBadge
                    + locHtml
                    + '</div></div></div>'
                    + '<div class="bak-backup-card-actions">'
                    + '<button class="fm-toolbar-btn btn-sm" data-preview="' + b.name + '"' + (b.path ? ' data-preview-path="' + b.path + '"' : '') + ` title="${t('Podgląd')}"><i class="fas fa-search"></i></button>`
                    + '<button class="fm-toolbar-btn btn-green btn-sm" data-restore="' + b.name + '"' + (b.path ? ' data-restore-path="' + b.path + '"' : '') + ` title="${t('Przywróć')}"><i class="fas fa-undo"></i></button>`
                    + '<button class="fm-toolbar-btn btn-red btn-sm" data-del-bak="' + b.name + '"' + (b.path ? ' data-del-path="' + b.path + '"' : '') + ` title="${t('Usuń')}"><i class="fas fa-trash"></i></button>`
                    + '</div></div>';
            });

            html += '</div></div>';
        });

        el.innerHTML = html;

        /* Toggle group collapse */
        el.querySelectorAll('.bak-group-header[data-bak-toggle]').forEach(function(hdr) {
            hdr.onclick = function() {
                var group = hdr.closest('.bak-group');
                group.classList.toggle('bak-group-collapsed');
            };
        });

        el.querySelectorAll('button[data-preview]').forEach(function(btn) {
            btn.onclick = function() { openPreview(btn.dataset.preview, btn.dataset.previewPath); };
        });
        el.querySelectorAll('button[data-restore]').forEach(function(btn) {
            btn.onclick = function() { startRestore(btn.dataset.restore, btn.dataset.restorePath); };
        });
        el.querySelectorAll('button[data-del-bak]').forEach(function(btn) {
            btn.onclick = async function() {
                if (!confirm(t('Usunąć kopię?'))) return;
                var url = '/backup/backups/' + btn.dataset.delBak;
                if (btn.dataset.delPath) url += '?path=' + encodeURIComponent(btn.dataset.delPath);
                await api(url, { method: 'DELETE' });
                loadBackups();
            };
        });
    }

    /* Preview modal */
    var previewFile = null;
    var previewPath = null;

    async function openPreview(filename, fullPath) {
        previewFile = filename;
        previewPath = fullPath || null;
        QS('#bak-preview-modal').classList.remove('hidden');
        QS('#bak-preview-body').innerHTML = `<p class="bak-text-muted"><i class="fas fa-spinner fa-spin"></i> ${t('Ładowanie podglądu...')}</p>`;
        try {
            var url = '/backup/backup-preview/' + encodeURIComponent(filename);
            if (fullPath) url += '?path=' + encodeURIComponent(fullPath);
            var data = await api(url);
            var html = '';

            // Header
            html += '<div class="bak-preview-header">'
                + '<h4 class="bak-m0">' + data.filename + '</h4>'
                + '<div class="bak-row-mt6-wrap">'
                + '<span class="bak-badge">' + (data.size/1048576).toFixed(1) + ' MB</span>'
                + (data.encrypted ? '' : '<span class="bak-badge">' + data.total_files + ` ${t('plików')}</span>`)
                + (data.encrypted ? '<span class="bak-badge bak-badge-orange"><i class="fas fa-lock"></i> Zaszyfrowany</span>' : (data.is_incremental ? '<span class="bak-badge bak-badge-purple">Przyrostowy</span>' : `<span class="bak-badge bak-badge-green">${t('Pełny')}</span>`))
                + '<span class="bak-badge">' + new Date(data.modified).toLocaleString('pl') + '</span>'
                + '</div></div>';

            // Encrypted notice
            if (data.encrypted) {
                html += '<div class="bak-encrypt-warning">'
                    + '<i class="fas fa-lock"></i> <strong>Backup jest zaszyfrowany (AES-256)</strong>'
                    + '<br>Podgląd zawartości niedostępny bez hasła. Możesz przywrócić backup — zostaniesz poproszony o hasło.'
                    + '</div>';
                QS('#bak-preview-body').innerHTML = html;
                return;
            }

            // Incremental chain
            if (data.chain && data.chain.length > 1) {
                html += '<div class="bak-preview-section">'
                    + `<h5><i class="fas fa-link"></i> ${t('Łańcuch inkrementalny (')}` + data.chain.length + ` ${t('archiwów)')}</h5>`
                    + '<div class="bak-chain-list">';
                data.chain.forEach(function(c, i) {
                    html += '<div class="bak-chain-item' + (c.name === filename ? ' bak-chain-current' : '') + '">'
                        + '<span class="bak-chain-num">' + (i+1) + '</span>'
                        + '<span class="bak-flex1-mono">' + c.name + '</span>'
                        + '<span>' + (c.size/1048576).toFixed(1) + ' MB</span> '
                        + (c.is_full ? '<span class="bak-badge bak-badge-green">Full</span>' : '<span class="bak-badge bak-badge-purple">Incr</span>')
                        + '</div>';
                });
                html += '</div></div>';
            }

            // Top directories
            if (data.top_dirs && Object.keys(data.top_dirs).length) {
                html += '<div class="bak-preview-section">'
                    + `<h5><i class="fas fa-folder-open"></i> ${t('Katalogi główne')}</h5>`
                    + '<div class="bak-dir-grid">';
                Object.entries(data.top_dirs).sort(function(a,b) { return b[1]-a[1]; }).forEach(function(entry) {
                    html += '<div class="bak-dir-item"><i class="fas fa-folder bak-icon-accent"></i> ' + entry[0] + ' <span class="bak-text-muted">(' + entry[1] + ')</span></div>';
                });
                html += '</div></div>';
            }

            // File list
            if (data.files && data.files.length) {
                html += '<div class="bak-preview-section">'
                    + '<h5><i class="fas fa-file"></i> Pliki ' + (data.truncated ? '(pierwsze ' + data.files.length + ' z ' + data.total_files + ')' : '(' + data.total_files + ')') + '</h5>'
                    + '<div class="bak-file-list">';
                data.files.forEach(function(f) {
                    var isDir = f.endsWith('/');
                    html += '<div class="bak-file-item">' + (isDir ? '<i class="fas fa-folder bak-icon-accent"></i>' : '<i class="fas fa-file bak-text-muted"></i>') + ' <span>' + f + '</span></div>';
                });
                if (data.truncated) html += '<div class="bak-file-item bak-text-muted-italic">... i ' + (data.total_files - data.files.length) + ` ${t('więcej')}</div>`;
                html += '</div></div>';
            }

            QS('#bak-preview-body').innerHTML = html;
        } catch(e) {
            QS('#bak-preview-body').innerHTML = `<p class="bak-text-danger">${t('Błąd ładowania podglądu')}</p>`;
        }
    }

    QS('#bak-preview-close').onclick = function() { QS('#bak-preview-modal').classList.add('hidden'); };
    QS('#bak-preview-restore').onclick = function() {
        QS('#bak-preview-modal').classList.add('hidden');
        if (previewFile) startRestore(previewFile, previewPath);
    };

    async function startRestore(filename, fullPath) {
        if (state.busy) { toast(t('Operacja w toku'), 'warning'); return; }
        var mode = QS('#bak-restore-mode-safe').checked ? 'safe' : 'original';
        var target = '';
        if (mode === 'safe') {
            target = QS('#bak-restore-target').value.trim();
            if (!target) {
                var now = new Date();
                target = '/data/restored_' + now.toISOString().slice(0,10).replace(/-/g, '');
            }
        }
        var msg = mode === 'original'
            ? t('UWAGA: Pliki zostaną przywrócone do oryginalnej lokalizacji. Nowsze wersje zostaną NADPISANE starszymi!') + '\n\n' + t('Przywrócić z "') + filename + '"?'
            : t('Pliki zostaną przywrócone do: ') + target + '\n\n' + t('Przywrócić z "') + filename + '"?';
        if (!confirm(msg)) return;

        // Prompt for passphrase if backup is encrypted
        var decryptPassphrase = null;
        if (filename.endsWith('.gpg')) {
            decryptPassphrase = await promptPassphrase('Hasło odszyfrowania backupu', false);
            if (!decryptPassphrase) return;
        }

        try {
            var body = { backup_file: filename, target_path: target };
            if (fullPath) body.backup_path = fullPath;
            if (decryptPassphrase) body.decrypt_passphrase = decryptPassphrase;
            await api('/backup/restore', { method: 'POST', body: body });
            state.busy = true;
            switchTab('backup');
            showProgress();
            toast(t('Przywracanie rozpoczęte'), 'info');
        } catch(e) { toast(t('Błąd'), 'error'); }
    }

    /* ══════════════════════════════════════════════════════════
       HISTORY
       ══════════════════════════════════════════════════════════ */
    async function loadHistory() {
        try { state.history = (await api('/backup/history')).history || []; renderHistory(); } catch(e) {}
    }
    function renderHistory() {
        var el = QS('#bak-history-list');
        if (!state.history.length) { el.innerHTML = `<div class="bak-empty-state"><i class="fas fa-history"></i><p>${t('Brak historii')}</p><span>${t('Historia operacji pojawi się tutaj')}</span></div>`; return; }

        function destBadge(h) {
            if (!h.destination) return '<span class="bak-badge">Lokalnie</span>';
            if (h.destination.type === 'usb') return '<span class="bak-badge bak-badge-blue"><i class="fas fa-usb"></i> USB</span>';
            if (h.destination.type === 'ssh') return '<span class="bak-badge bak-badge-purple"><i class="fas fa-server"></i> SSH</span>';
            return '<span class="bak-badge">Lokalnie</span>';
        }
        function opBadge(h) {
            if (h.operation === 'restore') return ' <span class="bak-badge bak-badge-green"><i class="fas fa-undo"></i> Przywrócenie</span>';
            return '';
        }

        var html = `<div class="bak-row-end"><button class="fm-toolbar-btn btn-red btn-sm" id="bak-clear-history"><i class="fas fa-trash"></i> ${t('Wyczyść historię')}</button></div>`;
        html += state.history.map(function(h) {
            var ok = h.status === 'completed';
            var dt = new Date(h.timestamp);
            var finalLoc = h.final_location ? prettyPath(h.final_location) : null;
            return '<div class="bak-history-card bak-hist-' + h.status + '">'
                + '<div class="bak-hist-status-icon"><i class="fas ' + (ok ? 'fa-check-circle' : 'fa-exclamation-triangle') + '"></i></div>'
                + '<div class="bak-hist-content">'
                + '<div class="bak-hist-title">' + (h.archive_file || 'backup') + opBadge(h) + ' ' + destBadge(h) + '</div>'
                + '<div class="bak-hist-details">'
                + '<span><i class="fas fa-calendar"></i> ' + dt.toLocaleString('pl') + '</span>'
                + '<span><i class="fas fa-file"></i> ' + (h.files_count || 0) + ` ${t('plików')}</span>`
                + '<span><i class="fas fa-weight-hanging"></i> ' + formatSize(h.size || 0) + '</span>'
                + '<span><i class="fas fa-stopwatch"></i> ' + formatDuration(h.duration) + '</span>'
                + '</div>'
                + (finalLoc ? '<div class="bak-hist-dest"><i class="fas fa-arrow-right"></i> Zapisano w: <strong>' + finalLoc + '</strong></div>' : '')
                + (h.error ? '<div class="bak-hist-error"><i class="fas fa-exclamation-circle"></i> ' + h.error + '</div>' : '')
                + '</div>'
                + '<button class="fm-toolbar-btn btn-red btn-sm" data-del-hist="' + h.id + `" title="${t('Usuń')}"><i class="fas fa-trash"></i></button>`
                + '</div>';
        }).join('');
        el.innerHTML = html;
        el.querySelectorAll('[data-del-hist]').forEach(function(btn) {
            btn.onclick = async function() {
                if (!confirm(t('Usunąć wpis z historii?'))) return;
                await api('/backup/history/' + btn.dataset.delHist, { method: 'DELETE' });
                loadHistory();
            };
        });
        var clearBtn = QS('#bak-clear-history');
        if (clearBtn) {
            clearBtn.onclick = async function() {
                if (!confirm(t('Usunąć całą historię?'))) return;
                await api('/backup/history', { method: 'DELETE' });
                loadHistory();
            };
        }
    }

    /* ══════════════════════════════════════════════════════════
       PROGRESS + LOGS via SocketIO
       ══════════════════════════════════════════════════════════ */
    function showProgress() {
        QS('#bak-progress').classList.remove('hidden');
        var cancelBtn = QS('#bak-cancel-btn');
        if (cancelBtn) cancelBtn.classList.remove('hidden');
        state.logs = [];
        QS('#bak-log-viewer').innerHTML = '';
    }

    function hideProgress() {
        var cancelBtn = QS('#bak-cancel-btn');
        if (cancelBtn) cancelBtn.classList.add('hidden');
    }

    QS('#bak-cancel-btn').onclick = async function() {
        if (!confirm(t('Na pewno anulować operację?'))) return;
        try {
            await api('/backup/cancel', { method: 'POST' });
            toast('Anulowano', 'warning');
        } catch(e) { toast(t('Błąd anulowania'), 'error'); }
    };
    QS('#bak-progress-close').onclick = function() {
        QS('#bak-progress').classList.add('hidden');
    };

    function switchTab(tab) {
        body.querySelectorAll('.bak-nav-item').forEach(function(t) {
            t.classList.toggle('active', t.dataset.tab === tab);
        });
        body.querySelectorAll('.bak-panel').forEach(function(p) { p.classList.remove('active'); });
        QS('#bak-panel-' + tab).classList.add('active');
        state.tab = tab;
    }

    function addLog(msg) {
        var viewer = QS('#bak-log-viewer');
        if (!viewer) return;
        var line = document.createElement('div');
        line.className = 'bak-log-line';
        line.textContent = msg;
        viewer.appendChild(line);
        while (viewer.children.length > 200) viewer.removeChild(viewer.firstChild);
        viewer.scrollTop = viewer.scrollHeight;
    }

    if (NAS.socket) {
        var onProgress = function(data) {
            state.busy = true;
            QS('#bak-progress').classList.remove('hidden');

            var pct = data.overall_percent || data.percent || 0;
            QS('#bak-progress-bar').style.width = pct + '%';
            QS('#bak-progress-pct').textContent = Math.round(pct) + '%';
            QS('#bak-progress-text').textContent = data.current_file || data.operation || '—';

            // Stage indicator
            var stageEl = QS('#bak-progress-stage');
            if (data.stage === 'transfer' || (pct > 0 && data.operation && data.operation.indexOf('transfer') !== -1)) {
                stageEl.textContent = 'Transfer';
                stageEl.className = 'bak-badge bak-badge-blue';
            } else {
                stageEl.textContent = t('Archiwizacja');
                stageEl.className = 'bak-badge bak-badge-green';
            }

            var info = [];
            if (data.files_done != null) info.push(data.files_done + '/' + (data.total_files || '?') + t(' plików'));
            if (data.eta != null) info.push('ETA: ' + Math.round(data.eta) + 's');
            if (data.stage) info.push(data.stage);
            QS('#bak-progress-info').textContent = info.join(' | ');
            updateStatusIndicator();

            // Global Task Progress
            if (NAS.taskProgress) {
                NAS.taskProgress.upsert({
                    id: 'backup_op', source: 'backup',
                    title: t('Kopia zapasowa'),
                    percent: Math.round(pct),
                    message: data.current_file || data.operation || '',
                    action: { app: 'backup', tab: 'history' }
                });
            }
        };

        var onLog = function(data) {
            var msg = typeof data === 'string' ? data : (data.message || data.line || JSON.stringify(data));
            addLog(msg);
        };

        var onComplete = function(data) {
            state.busy = false;
            toast(data.message || t('Operacja zakończona'), 'success');
            addLog('✓ ' + (data.message || t('Zakończono')));
            loadBackups(); loadHistory(); loadProfiles(); loadScheduled();
            setTimeout(renderDashboard, 300);
            hideProgress();
            updateStatusIndicator();
            if (NAS.taskProgress) NAS.taskProgress.finish('backup_op', true, data.message || t('Kopia zakończona pomyślnie'));
        };

        var onError = function(data) {
            state.busy = false;
            toast(data.message || t('Błąd operacji'), 'error');
            addLog(t('✗ BŁĄD: ') + (data.message || t('Nieznany błąd')));
            loadHistory();
            hideProgress();
            updateStatusIndicator();
            if (NAS.taskProgress) NAS.taskProgress.finish('backup_op', false, data.message || t('Błąd operacji'));
        };

        NAS.socket.on('backup_progress', onProgress);
        NAS.socket.on('backup_log', onLog);
        NAS.socket.on('backup_complete', onComplete);
        NAS.socket.on('backup_error', onError);

        var win = body.closest('.window');
        if (win) {
            var origClose = win._onClose;
            win._onClose = function() {
                NAS.socket.off('backup_progress', onProgress);
                NAS.socket.off('backup_log', onLog);
                NAS.socket.off('backup_complete', onComplete);
                NAS.socket.off('backup_error', onError);
                if (origClose) origClose();
            };
        }
    }

    function updateStatusIndicator() {
        var el = QS('#bak-status-indicator');
        if (state.busy) {
            el.innerHTML = '<i class="fas fa-spinner fa-spin bak-icon-accent"></i> Operacja w toku...';
            el.style.color = 'var(--accent)';
        } else {
            el.innerHTML = 'Gotowy';
            el.style.color = '';
        }
    }

    async function checkStatus() {
        try {
            var r = await api('/backup/status');
            state.busy = !!r.busy;
            updateStatusIndicator();
            if (state.busy) {
                showProgress();
                try {
                    var p = await api('/backup/progress');
                    if (p.progress) {
                        var data = p.progress;
                        var pct = data.overall_percent || data.percent || 0;
                        QS('#bak-progress-bar').style.width = pct + '%';
                        QS('#bak-progress-pct').textContent = Math.round(pct) + '%';
                        QS('#bak-progress-text').textContent = data.current_file || data.operation || '—';
                    }
                } catch(e2) {}
            }
        } catch(e) {}
    }

    /* ══════════════════════════════════════════════════════════
       INIT
       ══════════════════════════════════════════════════════════ */
    loadProfiles(); loadScheduled(); loadPaths(); loadBackups(); loadHistory(); loadUSB(); loadSSH(); checkStatus();
    // Update dashboard after data loads
    setTimeout(renderDashboard, 500);

    /* ══════════════════════════════════════════════════════════
       SNAPSHOTS — Punkty przywracania systemu
       ══════════════════════════════════════════════════════════ */
    var snapshots = [];
    var _snapRestoreId = null;

    async function loadSnapshots() {
        try {
            var r = await api('/backup/snapshots');
            snapshots = r.snapshots || [];
            renderSnapshots();
            loadSnapSpace();
        } catch(e) { console.error('loadSnapshots', e); }
    }

    async function loadSnapSpace() {
        try {
            var r = await api('/backup/snapshots/space');
            var box = QS('#bak-snap-space');
            if (!box) return;
            box.style.display = '';
            var pct = r.disk_total ? ((r.snapshot_bytes / r.disk_total) * 100) : 0;
            var bar = QS('#bak-snap-space-bar');
            bar.style.width = Math.min(pct, 100).toFixed(1) + '%';
            if (pct > 25) { bar.style.background = '#ef4444'; }
            else if (pct > 10) { bar.style.background = '#f59e0b'; }
            else { bar.style.background = '#10b981'; }
            QS('#bak-snap-space-text').textContent = formatSize(r.snapshot_bytes) + ' / ' + formatSize(r.disk_free) + ' ' + t('wolne') + ' (' + r.snapshot_count + ' ' + t('punktów') + ')';
            var hint = QS('#bak-snap-space-hint');
            if (pct > 25) hint.innerHTML = '<span class="bak-text-danger"><i class="fas fa-exclamation-triangle"></i> ' + t('Punkty przywracania zajmują ponad 25% dysku! Rozważ usunięcie starszych.') + '</span>';
            else if (pct > 10) hint.innerHTML = '<span class="bak-text-warning"><i class="fas fa-exclamation-triangle"></i> ' + t('Punkty zajmują ponad 10% dysku — rozważ zmniejszenie ich liczby.') + '</span>';
            else hint.textContent = '';
        } catch(e) { /* ignore — space endpoint may not be available */ }
    }

    function renderSnapshots() {
        var el = QS('#snap-list');
        if (!snapshots.length) {
            el.innerHTML = `<div class="bak-empty-lg"><i class="fas fa-camera-retro bak-empty-icon"></i>${t('Brak punktów przywracania.')}<br>${t('Utwórz pierwszy punkt przywracania.')}</div>`;
            return;
        }
        var html = '';
        snapshots.forEach(function(s, idx) {
            var dt = s.created ? new Date(s.created) : null;
            var dateStr = dt ? dt.toLocaleString(getLocale()) : '?';
            var sizeStr = formatSize(s.size || 0);
            var isReceived = !!s.received;
            var inc = s.includes || {};
            var badges = '';
            if (isReceived) badges += '<span class="bak-badge bak-badge-orange"><i class="fas fa-globe"></i> ' + t('Otrzymany z:') + ' ' + (s.hostname||t('inny NAS')) + '</span> ';
            if (inc.ethos) badges += '<span class="bak-badge bak-fs10"><i class="fas fa-server"></i> ' + t('Ustawienia') + '</span> ';
            if (inc.system) badges += '<span class="bak-badge bak-fs10"><i class="fas fa-cog"></i> ' + t('System') + '</span> ';
            if (inc.docker) badges += '<span class="bak-badge bak-badge-blue bak-fs10"><i class="fab fa-docker"></i> Docker (' + (s.docker_projects||[]).length + ')</span> ';
            if (inc.volumes) badges += '<span class="bak-badge bak-badge-green bak-fs10"><i class="fas fa-database"></i> ' + t('Dane') + ' (' + (s.docker_volumes||[]).length + ')</span> ';

            var borderStyle = isReceived ? 'border-left:4px solid #e67e22;' : '';
            html += '<div class="bak-item bak-backup-item" style="' + borderStyle + '">';
            html += '<div class="bak-flex1">';
            html += '<div class="bak-fw600">' + (s.label || s.id) + '</div>';
            html += '<div class="bak-small12-muted-mt">' + dateStr + ' · ' + sizeStr + '</div>';
            html += '<div class="bak-mt6">' + badges + '</div>';
            html += '</div>';
            html += '<div class="bak-row-noshrink">';
            if (isReceived) {
                // Received snapshot: restore from source + adopt buttons
                html += '<button class="fm-toolbar-btn btn-green snap-rcv-restore-btn" data-idx="' + idx + `" title="${t('Przywróć z tego punktu')}"><i class="fas fa-undo"></i></button>`;
                html += '<button class="fm-toolbar-btn snap-rcv-adopt-btn bak-text-orange" data-idx="' + idx + '" title="' + t('Skopiuj do lokalnych') + '"><i class="fas fa-plus-circle"></i></button>';
            } else {
                // Local snapshot: browse, transfer, restore, download, delete
                html += '<button class="fm-toolbar-btn snap-browse-btn" data-id="' + s.dir + `" title="${t('Przeglądaj zawartość')}"><i class="fas fa-folder-open"></i></button>`;
                html += '<button class="fm-toolbar-btn snap-transfer-btn bak-icon-accent" data-id="' + s.dir + `" title="${t('Wyślij na inny NAS')}"><i class="fas fa-share"></i></button>`;
                html += '<button class="fm-toolbar-btn btn-green snap-restore-btn" data-id="' + s.dir + `" title="${t('Przywróć')}"><i class="fas fa-undo"></i></button>`;
                html += '<button class="fm-toolbar-btn snap-download-btn" data-id="' + s.dir + '" title="' + t('Pobierz') + '"><i class="fas fa-download"></i></button>';
                html += '<button class="fm-toolbar-btn bak-text-red" data-id="' + s.dir + `" title="${t('Usuń')}" onclick="deleteSnapshot(this)"><i class="fas fa-trash"></i></button>`;
            }
            html += '</div></div>';
        });
        el.innerHTML = html;

        // Local snapshot buttons
        el.querySelectorAll('.snap-browse-btn').forEach(function(btn) {
            btn.onclick = function() { openSnapBrowse(btn.dataset.id); };
        });
        el.querySelectorAll('.snap-restore-btn').forEach(function(btn) {
            btn.onclick = function() { openRestoreModal(btn.dataset.id); };
        });
        el.querySelectorAll('.snap-transfer-btn').forEach(function(btn) {
            btn.onclick = function() { openTransferModal(btn.dataset.id); };
        });
        el.querySelectorAll('.snap-download-btn').forEach(function(btn) {
            btn.onclick = function() {
                var a = document.createElement('a');
                a.href = '/api/backup/snapshots/' + btn.dataset.id + '/download';
                a.download = '';
                a.click();
            };
        });
        el.querySelectorAll('button[onclick="deleteSnapshot(this)"]').forEach(function(btn) {
            btn.onclick = async function() {
                if (!confirm(t('Usunąć punkt przywracania ') + btn.dataset.id + '?')) return;
                try {
                    await api('/backup/snapshots/' + btn.dataset.id, { method: 'DELETE' });
                    NAS.toast(t('Punkt przywracania usunięty'), 'success');
                    loadSnapshots();
                } catch(e) { NAS.toast(t('Błąd: ') + e.message, 'error'); }
            };
        });

        // Received snapshot buttons
        el.querySelectorAll('.snap-rcv-restore-btn').forEach(function(btn) {
            btn.onclick = function() { openReceivedRestoreModal(parseInt(btn.dataset.idx)); };
        });
        el.querySelectorAll('.snap-rcv-adopt-btn').forEach(function(btn) {
            btn.onclick = async function() {
                var idx = parseInt(btn.dataset.idx);
                var s = snapshots[idx];
                if (!s || !s.source_path) return;
                if (!confirm(t('Skopiować „{name}” do lokalnych punktów przywracania?').replace('{name}', s.label||s.id))) return;
                try {
                    await api('/backup/snapshots/received/adopt', {
                        method: 'POST',
                        headers: {'Content-Type': 'application/json'},
                        body: JSON.stringify({source_path: s.source_path})
                    });
                    NAS.toast(t('Punkt skopiowany do lokalnych!'), 'success');
                    loadSnapshots();
                } catch(e) { NAS.toast(t('Błąd: ') + (e.message||e), 'error'); }
            };
        });
    }

    // ── Browse snapshot contents ──
    QS('#snap-browse-close').onclick = function() { QS('#snap-browse-modal').classList.add('hidden'); };

    async function openSnapBrowse(snapId) {
        var snap = snapshots.find(function(s) { return s.dir === snapId; });
        if (!snap) return;
        var dt = snap.created ? new Date(snap.created).toLocaleString(getLocale()) : '?';
        QS('#snap-browse-info').innerHTML =
            '<i class="fas fa-camera-retro bak-icon-accent"></i> <b>' + (snap.label || snap.id) + '</b>' +
            '<span class="bak-text-muted-ml8">' + dt + ' · ' + formatSize(snap.size || 0) + '</span>';
        QS('#snap-browse-tree').innerHTML = '<div class="bak-empty-sm"><i class="fas fa-spinner fa-spin"></i> ' + t('Ładowanie...') + '</div>';
        QS('#snap-browse-modal').classList.remove('hidden');
        try {
            var r = await api('/backup/snapshots/' + snapId + '/browse');
            var items = r.tree || [];
            if (!items.length) {
                QS('#snap-browse-tree').innerHTML = '<div class="bak-empty-sm">' + t('Pusty snapshot') + '</div>';
                return;
            }
            // Build tree structure
            var dirs = items.filter(function(i) { return i.type === 'dir'; });
            var files = items.filter(function(i) { return i.type === 'file'; });
            // Group files by first directory level
            var grouped = {};
            files.forEach(function(f) {
                var parts = f.name.split('/');
                var top = parts.length > 1 ? parts[0] : '';
                if (!grouped[top]) grouped[top] = [];
                grouped[top].push(f);
            });
            var html = '';
            // Top-level dirs
            var topDirs = dirs.filter(function(d) { return d.name.indexOf('/') === -1; });
            topDirs.forEach(function(d) {
                var dirFiles = (grouped[d.name] || []);
                var totalSize = dirFiles.reduce(function(s, f) { return s + (f.size || 0); }, 0);
                html += '<details class="snap-browse-dir">'
                    + '<summary><i class="fas fa-folder bak-icon-accent-mr"></i><strong>' + d.name + '/</strong>'
                    + '<span class="bak-text-muted-ml">' + dirFiles.length + ' ' + t('plików') + ' · ' + formatSize(totalSize) + '</span></summary>'
                    + '<div class="snap-browse-files">';
                dirFiles.forEach(function(f) {
                    var fname = f.name.split('/').pop();
                    html += '<div class="snap-browse-file">'
                        + '<i class="fas fa-file bak-text-muted-mr"></i>'
                        + '<span>' + fname + '</span>'
                        + '<span class="bak-text-muted-right">' + formatSize(f.size) + '</span>'
                        + '</div>';
                });
                html += '</div></details>';
            });
            // Root-level files
            (grouped[''] || []).forEach(function(f) {
                html += '<div class="snap-browse-file">'
                    + '<i class="fas fa-file bak-text-muted-mr"></i>'
                    + '<span>' + f.name + '</span>'
                    + '<span class="bak-text-muted-right">' + formatSize(f.size) + '</span>'
                    + '</div>';
            });
            QS('#snap-browse-tree').innerHTML = html;
        } catch(e) {
            QS('#snap-browse-tree').innerHTML = '<div class="bak-empty-danger-sm">' + t('Błąd: ') + (e.message || e) + '</div>';
        }
    }

    // Create modal
    QS('#snap-create-btn').onclick = function() {
        QS('#snap-label').value = '';
        QS('#snap-inc-ethos').checked = true;
        QS('#snap-inc-system').checked = true;
        QS('#snap-inc-docker').checked = true;
        QS('#snap-inc-volumes').checked = true;
        QS('#snap-dest-type').value = 'local';
        QS('#snap-dest-usb').classList.add('hidden');
        loadSnapUSB();
        QS('#snap-create-modal').classList.remove('hidden');
    };
    QS('#snap-create-close').onclick = QS('#snap-create-cancel').onclick = function() {
        QS('#snap-create-modal').classList.add('hidden');
    };
    QS('#snap-dest-type').onchange = function() {
        QS('#snap-dest-usb').classList.toggle('hidden', this.value !== 'usb');
    };

    async function loadSnapUSB() {
        try {
            var r = await api('/backup/usb-drives');
            var sel = QS('#snap-dest-usb');
            sel.innerHTML = '';
            (r.drives || []).forEach(function(d) {
                sel.innerHTML += '<option value="' + d.path + '">' + d.label + ' (' + d.path + ')</option>';
            });
        } catch(e) {}
    }

    QS('#snap-create-go').onclick = async function() {
        QS('#snap-create-modal').classList.add('hidden');
        var payload = {
            label: QS('#snap-label').value.trim(),
            include_ethos: QS('#snap-inc-ethos').checked,
            include_system: QS('#snap-inc-system').checked,
            include_docker: QS('#snap-inc-docker').checked,
            include_volumes: QS('#snap-inc-volumes').checked,
            dest_type: QS('#snap-dest-type').value,
            dest_path: QS('#snap-dest-type').value === 'usb' ? QS('#snap-dest-usb').value : '',
        };
        try {
            await api('/backup/snapshots', { method: 'POST', body: payload });
            NAS.toast(t('Tworzenie snapshota rozpoczęte'), 'info');
            showSnapProgress();
            pollSnapStatus();
        } catch(e) {
            NAS.toast(t('Błąd: ') + (e.message || e), 'error');
        }
    };

    function showSnapProgress() {
        QS('#snap-progress').classList.remove('hidden');
        QS('#snap-progress-log').textContent = '';
        QS('#snap-progress-bar').style.width = '0%';
        QS('#snap-progress-pct').textContent = '0%';
        QS('#snap-progress-msg').textContent = 'Rozpoczynam...';
    }

    function pollSnapStatus() {
        var iv = setInterval(async function() {
            try {
                var r = await api('/backup/snapshots/status');
                QS('#snap-progress-bar').style.width = (r.percent || 0) + '%';
                QS('#snap-progress-pct').textContent = Math.round(r.percent || 0) + '%';
                QS('#snap-progress-msg').textContent = r.message || '';
                if (r.log && r.log.length) {
                    QS('#snap-progress-log').textContent = r.log.join('\n');
                    QS('#snap-progress-log').scrollTop = QS('#snap-progress-log').scrollHeight;
                }
                if (r.status === 'done') {
                    clearInterval(iv);
                    NAS.toast(r.message || t('Punkt przywracania gotowy!'), 'success');
                    if (NAS.taskProgress) NAS.taskProgress.finish('snapshot_op', true, r.message || t('Punkt przywracania gotowy'));
                    setTimeout(function() {
                        QS('#snap-progress').classList.add('hidden');
                        loadSnapshots();
                        renderDashboard();
                    }, 2000);
                } else if (r.status === 'error') {
                    clearInterval(iv);
                    NAS.toast(r.message || t('Błąd punktu przywracania'), 'error');
                    if (NAS.taskProgress) NAS.taskProgress.finish('snapshot_op', false, r.message || t('Błąd'));
                }
            } catch(e) {}
        }, 1500);
    }

    // Socket events for real-time log
    if (NAS.socket) {
        NAS.socket.on('snapshot_log', function(data) {
            var logEl = QS('#snap-progress-log');
            if (logEl && !QS('#snap-progress').classList.contains('hidden')) {
                logEl.textContent += (data.message || '') + '\n';
                logEl.scrollTop = logEl.scrollHeight;
            }
        });
        NAS.socket.on('snapshot_progress', function(data) {
            if (QS('#snap-progress').classList.contains('hidden')) showSnapProgress();
            QS('#snap-progress-bar').style.width = (data.percent || 0) + '%';
            QS('#snap-progress-pct').textContent = Math.round(data.percent || 0) + '%';
            QS('#snap-progress-msg').textContent = data.message || '';
            if (NAS.taskProgress) {
                NAS.taskProgress.upsert({
                    id: 'snapshot_op', source: 'backup',
                    title: t('Punkt przywracania'),
                    percent: Math.round(data.percent || 0),
                    message: data.message || '',
                    action: { app: 'backup', tab: 'snapshots' }
                });
            }
        });
    }

    // Restore modal
    function openRestoreModal(snapId) {
        _snapRestoreId = snapId;
        var snap = snapshots.find(function(s) { return s.dir === snapId; });
        if (!snap) return;
        var inc = snap.includes || {};
        var dt = snap.created ? new Date(snap.created).toLocaleString(getLocale()) : '?';
        QS('#snap-restore-info').innerHTML =
            '<b>' + (snap.label || snap.id) + '</b><br>' +
            '<span class="bak-text-muted">' + dt + '</span>';
        QS('#snap-rst-ethos').checked = !!inc.ethos;
        QS('#snap-rst-ethos').disabled = !inc.ethos;
        QS('#snap-rst-system').checked = !!inc.system;
        QS('#snap-rst-system').disabled = !inc.system;
        QS('#snap-rst-docker').checked = !!inc.docker;
        QS('#snap-rst-docker').disabled = !inc.docker;
        QS('#snap-rst-volumes').checked = !!inc.volumes;
        QS('#snap-rst-volumes').disabled = !inc.volumes;
        var ci = QS('#snap-restore-confirm-input');
        if (ci) { ci.value = ''; }
        var goBtn = QS('#snap-restore-go');
        goBtn.style.opacity = '.5';
        goBtn.style.pointerEvents = 'none';
        QS('#snap-restore-modal').classList.remove('hidden');
    }
    // Enable button only after typing "PRZYWRÓĆ"
    var _snapConfirmInput = QS('#snap-restore-confirm-input');
    if (_snapConfirmInput) {
        _snapConfirmInput.oninput = function() {
            var val = this.value.trim().toUpperCase();
            var goBtn = QS('#snap-restore-go');
            if (val === 'PRZYWRÓĆ' || val === 'PRZYWROC') {
                goBtn.style.opacity = '1';
                goBtn.style.pointerEvents = '';
            } else {
                goBtn.style.opacity = '.5';
                goBtn.style.pointerEvents = 'none';
            }
        };
    }
    QS('#snap-restore-close').onclick = QS('#snap-restore-cancel').onclick = function() {
        QS('#snap-restore-modal').classList.add('hidden');
    };
    QS('#snap-restore-go').onclick = async function() {
        if (!_snapRestoreId) return;
        QS('#snap-restore-modal').classList.add('hidden');
        try {
            await api('/backup/snapshots/' + _snapRestoreId + '/restore', { method: 'POST', body: {
                restore_ethos: QS('#snap-rst-ethos').checked,
                restore_system: QS('#snap-rst-system').checked,
                restore_docker: QS('#snap-rst-docker').checked,
                restore_volumes: QS('#snap-rst-volumes').checked,
            }});
            NAS.toast(t('Przywracanie rozpoczęte'), 'info');
            showSnapProgress();
            pollSnapStatus();
        } catch(e) {
            NAS.toast(t('Błąd: ') + (e.message || e), 'error');
        }
    };

    // Check on tab switch if snapshot operation is in progress
    var origTabClick = null;
    body.querySelectorAll('.bak-nav-item').forEach(function(tab) {
        if (tab.dataset.tab === 'snapshots') {
            tab.addEventListener('click', function() {
                loadSnapshots();
                // Check if operation in progress
                api('/backup/snapshots/status').then(function(r) {
                    if (r.status === 'creating' || r.status === 'restoring' || r.status === 'transferring') {
                        showSnapProgress();
                        pollSnapStatus();
                    }
                }).catch(function(){});
            });
        }
    });

    // ── Import snapshot from file ──
    QS('#snap-import-btn').onclick = function() {
        QS('#snap-import-file').click();
    };
    QS('#snap-import-file').onchange = async function() {
        var file = this.files[0];
        if (!file) return;
        this.value = '';
        if (!file.name.endsWith('.tar.gz')) {
            NAS.toast('Wymagany plik .tar.gz', 'error');
            return;
        }
        NAS.toast(t('Importuję snapshot (') + formatSize(file.size) + ')...', 'info');
        showSnapProgress();
        QS('#snap-progress-msg').textContent = t('Przesyłanie pliku...');
        try {
            var fd = new FormData();
            fd.append('file', file);
            var resp = await fetch('/api/backup/snapshots/import', {
                method: 'POST',
                body: fd,
                credentials: 'same-origin'
            });
            var r = await resp.json();
            if (r.ok) {
                NAS.toast('Snapshot zaimportowany!', 'success');
                QS('#snap-progress').classList.add('hidden');
                loadSnapshots();
            } else {
                NAS.toast(t('Błąd: ') + (r.error || '?'), 'error');
                QS('#snap-progress').classList.add('hidden');
            }
        } catch(e) {
            NAS.toast(t('Błąd importu: ') + e.message, 'error');
            QS('#snap-progress').classList.add('hidden');
        }
    };

    // ── Transfer modal ──
    var _snapTransferId = null;

    async function loadSSHServersForSelect(selectEl) {
        try {
            var r = await api('/backup/ssh-servers');
            selectEl.innerHTML = '<option value="">-- wybierz serwer --</option>';
            (r.servers || []).forEach(function(s) {
                selectEl.innerHTML += '<option value="' + s.id + '">' + s.name + ' (' + s.host + ':' + (s.port || 22) + ')</option>';
            });
        } catch(e) {
            selectEl.innerHTML = `<option value="">${t('Brak serwerów SSH')}</option>`;
        }
    }

    QS('#snap-transfer-close').onclick = QS('#snap-transfer-cancel').onclick = function() {
        QS('#snap-transfer-modal').classList.add('hidden');
    };

    QS('#snap-transfer-go').onclick = async function() {
        if (!_snapTransferId) return;
        var serverId = QS('#snap-transfer-server').value;
        if (!serverId) {
            NAS.toast('Wybierz serwer docelowy', 'error');
            return;
        }
        QS('#snap-transfer-modal').classList.add('hidden');
        try {
            await api('/backup/snapshots/' + _snapTransferId + '/transfer', { method: 'POST', body: {server_id: serverId} });
            NAS.toast(t('Transfer rozpoczęty'), 'info');
            showSnapProgress();
            pollSnapStatus();
        } catch(e) {
            NAS.toast(t('Błąd: ') + (e.message || e), 'error');
        }
    };

    // ── Remote snapshots ──
    QS('#snap-remote-btn').onclick = function() {
        loadSSHServersForSelect(QS('#snap-remote-server'));
        QS('#snap-remote-list').innerHTML = `<div class="bak-empty">${t('Wybierz serwer i kliknij "Załaduj"')}</div>`;
        QS('#snap-remote-modal').classList.remove('hidden');
    };
    QS('#snap-remote-close').onclick = function() {
        QS('#snap-remote-modal').classList.add('hidden');
    };

    QS('#snap-remote-load').onclick = async function() {
        var serverId = QS('#snap-remote-server').value;
        if (!serverId) {
            NAS.toast('Wybierz serwer', 'error');
            return;
        }
        QS('#snap-remote-list').innerHTML = `<div class="bak-empty"><i class="fas fa-spinner fa-spin"></i> ${t('Ładowanie...')}</div>`;
        try {
            var r = await api('/backup/snapshots/remote', { method: 'POST', body: {server_id: serverId} });
            var snaps = r.snapshots || [];
            if (!snaps.length) {
                QS('#snap-remote-list').innerHTML = `<div class="bak-empty">${t('Brak snapshotów na')} ` + (r.host || 'zdalnym serwerze') + '</div>';
                return;
            }
            var html = '';
            snaps.forEach(function(s) {
                var dt = s.created ? new Date(s.created).toLocaleString(getLocale()) : '?';
                var inc = s.includes || {};
                var tags = '';
                if (inc.ethos) tags += '<span class="bak-badge bak-fs10">' + t('Ustawienia') + '</span> ';
                if (inc.docker) tags += '<span class="bak-badge bak-badge-blue bak-fs10">Docker</span> ';
                if (inc.volumes) tags += '<span class="bak-badge bak-badge-green bak-fs10">' + t('Dane') + '</span> ';
                html += '<div class="bak-list-item-sm">';
                html += '<div class="bak-flex1">';
                html += '<div class="bak-heading13">' + (s.label || s.id) + '</div>';
                html += '<div class="bak-small-muted">' + dt + ' · ' + formatSize(s.size || 0) + '</div>';
                html += '<div class="bak-mt4">' + tags + '</div>';
                html += '</div>';
                html += '<button class="fm-toolbar-btn btn-green snap-pull-btn" data-id="' + s.dir + '" data-server="' + serverId + '" title="' + t('Pobierz na ten NAS') + '"><i class="fas fa-download"></i></button>';
                html += '</div>';
            });
            QS('#snap-remote-list').innerHTML = html;
            QS('#snap-remote-list').querySelectorAll('.snap-pull-btn').forEach(function(btn) {
                btn.onclick = async function() {
                    if (!confirm(t('Pobrać punkt przywracania ') + btn.dataset.id + ' ' + t('ze zdalnego NAS?'))) return;
                    QS('#snap-remote-modal').classList.add('hidden');
                    try {
                        await api('/backup/snapshots/pull', { method: 'POST', body: {server_id: btn.dataset.server, snap_id: btn.dataset.id} });
                        NAS.toast(t('Pobieranie rozpoczęte'), 'info');
                        showSnapProgress();
                        pollSnapStatus();
                    } catch(e) {
                        NAS.toast(t('Błąd: ') + (e.message || e), 'error');
                    }
                };
            });
        } catch(e) {
            QS('#snap-remote-list').innerHTML = `<div class="bak-empty-danger">${t('Błąd:')} ` + (e.message || e) + '</div>';
        }
    };

    // ── NAS discovery ──
    var _discoveredNAS = [];
    var _addNasDevice = null;

    QS('#snap-discover-btn').onclick = function() {
        QS('#snap-discover-modal').classList.remove('hidden');
    };
    QS('#snap-discover-close').onclick = function() {
        QS('#snap-discover-modal').classList.add('hidden');
    };

    QS('#snap-discover-scan').onclick = async function() {
        var btn = QS('#snap-discover-scan');
        btn.disabled = true;
        btn.innerHTML = '<i class="fas fa-spinner fa-spin"></i> Skanowanie...';
        QS('#snap-discover-status').textContent = 'Skanowanie sieci (mDNS + port scan)...';
        QS('#snap-discover-list').innerHTML = '<div class="bak-empty"><i class="fas fa-spinner fa-spin bak-fs24"></i><br>Szukam EthOS w sieci...</div>';

        try {
            var r = await api('/backup/discover-nas', { method: 'POST' });
            _discoveredNAS = r.devices || [];
            renderDiscoveredNAS();
            QS('#snap-discover-status').textContent = 'Znaleziono: ' + _discoveredNAS.length + t(' urządzeń');
        } catch(e) {
            QS('#snap-discover-list').innerHTML = `<div class="bak-empty-danger">${t('Błąd:')} ` + (e.message || e) + '</div>';
            QS('#snap-discover-status').textContent = '';
        }
        btn.disabled = false;
        btn.innerHTML = `<i class="fas fa-search"></i> ${t('Skanuj sieć')}`;
    };

    function renderDiscoveredNAS() {
        var el = QS('#snap-discover-list');
        if (!_discoveredNAS.length) {
            el.innerHTML = '<div class="bak-empty">Nie znaleziono innych instancji EthOS w sieci</div>';
            return;
        }
        var html = '';
        _discoveredNAS.forEach(function(d) {
            var srcBadge = d.source === 'mdns'
                ? '<span class="bak-badge bak-badge-green bak-fs10"><i class="fas fa-broadcast-tower"></i> mDNS</span>'
                : '<span class="bak-badge bak-fs10"><i class="fas fa-search"></i> Skan</span>';
            html += '<div class="bak-list-item">';
            html += '<div class="bak-flex1">';
            html += '<div class="bak-fw600"><i class="fas fa-server bak-icon-accent-mr"></i>' + (d.name || '?') + '</div>';
            html += '<div class="bak-small-muted-mt2">' + d.ip + ':' + d.port + ' · ' + (d.hostname || '') + ' · v' + (d.version || '?') + ' ' + srcBadge + '</div>';
            html += '</div>';
            html += '<button class="fm-toolbar-btn btn-green snap-addnas-btn" data-ip="' + d.ip + '" data-port="' + d.port + '" data-name="' + (d.name || d.hostname || d.ip) + '" title="Dodaj jako cel"><i class="fas fa-plus"></i> Dodaj</button>';
            html += '</div>';
        });
        el.innerHTML = html;

        el.querySelectorAll('.snap-addnas-btn').forEach(function(btn) {
            btn.onclick = function() {
                _addNasDevice = {
                    name: btn.dataset.name,
                    ip: btn.dataset.ip,
                    port: parseInt(btn.dataset.port) || 22
                };
                QS('#snap-addnas-info').innerHTML =
                    '<i class="fas fa-server bak-icon-accent"></i> <b>' + _addNasDevice.name + '</b><br>' +
                    '<span class="bak-text-muted">' + _addNasDevice.ip + '</span>';
                QS('#snap-addnas-user').value = '';
                QS('#snap-addnas-pass').value = '';
                QS('#snap-addnas-path').value = '~/backups';
                QS('#snap-addnas-modal').classList.remove('hidden');
            };
        });
    }

    // Add discovered NAS as SSH server
    QS('#snap-addnas-close').onclick = QS('#snap-addnas-cancel').onclick = function() {
        QS('#snap-addnas-modal').classList.add('hidden');
    };

    QS('#snap-addnas-go').onclick = async function() {
        if (!_addNasDevice) return;
        var user = QS('#snap-addnas-user').value.trim();
        var pass = QS('#snap-addnas-pass').value;
        var rpath = QS('#snap-addnas-path').value.trim() || '/backups';
        if (!user) {
            NAS.toast(t('Podaj użytkownika SSH'), 'error');
            return;
        }
        var goBtn = QS('#snap-addnas-go');
        goBtn.disabled = true;
        goBtn.innerHTML = `<i class="fas fa-spinner fa-spin"></i> ${t('Testuję...')}`;
        try {
            // Test connection first
            var testR = await api('/backup/ssh-servers/test', { method: 'POST', body: {
                host: _addNasDevice.ip,
                port: 22,
                username: user,
                password: pass
            }});
            if (!testR.success) {
                NAS.toast(t('Nie można połączyć: ') + (testR.error || '?'), 'error');
                return;
            }
            // Add as SSH server
            await api('/backup/ssh-servers', { method: 'POST', body: {
                name: _addNasDevice.name,
                host: _addNasDevice.ip,
                port: 22,
                username: user,
                password: pass,
                remote_path: rpath
            }});
            NAS.toast('NAS dodany jako serwer docelowy ✓', 'success');
            QS('#snap-addnas-modal').classList.add('hidden');
            // Mark this device in the discover list
            renderDiscoveredNAS();
        } catch(e) {
            NAS.toast(t('Błąd: ') + (e.message || e), 'error');
        } finally {
            goBtn.disabled = false;
            goBtn.innerHTML = '<i class="fas fa-check"></i> Dodaj i testuj';
        }
    };

    // Also populate discovered NAS in transfer modal
    function openTransferModal(snapId) {
        _snapTransferId = snapId;
        var snap = snapshots.find(function(s) { return s.dir === snapId; });
        if (!snap) return;
        var dt = snap.created ? new Date(snap.created).toLocaleString(getLocale()) : '?';
        QS('#snap-transfer-info').innerHTML =
            '<b>' + (snap.label || snap.id) + '</b><br>' +
            '<span class="bak-text-muted">' + dt + ' · ' + formatSize(snap.size || 0) + '</span>';
        loadSSHServersForSelect(QS('#snap-transfer-server'));
        // Show quick-discovered NAS hint
        var discDiv = QS('#snap-transfer-discovered');
        if (_discoveredNAS.length) {
            discDiv.innerHTML = '<div class="bak-small-muted-mt"><i class="fas fa-info-circle"></i> ' + _discoveredNAS.length + ' NAS wykrytych w sieci — dodaj je przez przycisk "Wykryj NAS"</div>';
        } else {
            discDiv.innerHTML = '';
        }
        QS('#snap-transfer-modal').classList.remove('hidden');
    }

    // ── Received snapshots restore support ──
    var _rcvRestoreSnap = null;

    function openReceivedRestoreModal(idx) {
        var s = snapshots[idx];
        if (!s) return;
        _rcvRestoreSnap = s;
        var inc = s.includes || {};
        var dt = s.created ? new Date(s.created).toLocaleString(getLocale()) : '?';
        QS('#snap-rcv-restore-info').innerHTML =
            '<div class="bak-row-gap8"><i class="fas fa-inbox bak-icon-amber"></i> <b>' + (s.label || s.id) + '</b></div>' +
            '<div class="bak-text-muted-mt">' + dt + ` ${t('· Host źródłowy:')} <b>` + (s.hostname || '?') + '</b> · ' + formatSize(s.size || 0) + '</div>';
        QS('#snap-rcv-rst-ethos').checked = !!inc.ethos;
        QS('#snap-rcv-rst-ethos').disabled = !inc.ethos;
        QS('#snap-rcv-rst-system').checked = !!inc.system;
        QS('#snap-rcv-rst-system').disabled = !inc.system;
        QS('#snap-rcv-rst-docker').checked = !!inc.docker;
        QS('#snap-rcv-rst-docker').disabled = !inc.docker;
        QS('#snap-rcv-rst-volumes').checked = !!inc.volumes;
        QS('#snap-rcv-rst-volumes').disabled = !inc.volumes;
        QS('#snap-received-restore-modal').classList.remove('hidden');
    }
    QS('#snap-rcv-restore-close').onclick = QS('#snap-rcv-restore-cancel').onclick = function() {
        QS('#snap-received-restore-modal').classList.add('hidden');
    };
    QS('#snap-rcv-restore-go').onclick = async function() {
        if (!_rcvRestoreSnap) return;
        if (!confirm(t('Na pewno przywrócić system z otrzymanego snapshota? Obecna konfiguracja zostanie nadpisana.'))) return;
        QS('#snap-received-restore-modal').classList.add('hidden');
        try {
            await api('/backup/snapshots/received/restore', { method: 'POST', body: {
                source_path: _rcvRestoreSnap.source_path,
                restore_ethos: QS('#snap-rcv-rst-ethos').checked,
                restore_system: QS('#snap-rcv-rst-system').checked,
                restore_docker: QS('#snap-rcv-rst-docker').checked,
                restore_volumes: QS('#snap-rcv-rst-volumes').checked,
            }});
            NAS.toast(t('Przywracanie rozpoczęte'), 'info');
            showSnapProgress();
            pollSnapStatus();
        } catch(e) {
            NAS.toast(t('Błąd: ') + (e.message || e), 'error');
        }
    };

    QS('#snap-received-refresh').onclick = function() { loadSnapshots(); };

    // Initial load snapshots if tab is visible
    loadSnapshots();
}
