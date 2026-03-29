/**
 * EthOS — Menedżer pobierania (Download Manager)
 * Download manager with debrid service support
 */

AppRegistry['download-manager'] = function (appDef, launchOpts) {
    const winId = 'download-manager';
    if (WM.windows.has(winId)) {
        // If re-opened with a URL, add it
        if (launchOpts?.url) {
            _dlmAddUrl(launchOpts.url);
        }
        return;
    }

    createWindow(winId, {
        title: t('Menedżer pobierania'),
        icon: appDef?.icon || 'fa-cloud-download-alt',
        iconColor: appDef?.color || '#10b981',
        width: 850,
        height: 560,
        minWidth: 600,
        minHeight: 400,
        onRender: (body) => renderDownloadManager(body, launchOpts),
    });
};

/* Global helper to add URL from outside */
function _dlmAddUrl(url) {
    const inp = document.querySelector('#dlm-url-input');
    if (inp) {
        const current = inp.value.trim();
        inp.value = current ? current + '\n' + url : url;
        inp.style.height = 'auto';
        inp.style.height = Math.min(inp.scrollHeight, 160) + 'px';
    }
}

function renderDownloadManager(body, launchOpts) {
    let currentTab = 'downloads'; // 'downloads' | 'settings'
    let downloads = [];
    let config = {};
    let filterText = '';
    let metricsCache = {};
    const speedSamples = [];
    const SPEED_WINDOW_MS = 10 * 60 * 1000;
    const SPEED_SAMPLE_MS = 5000;
    let speedSampleTimer = null;
    let statsTimer = null;

    const forcedDestDir = (launchOpts?.dest_dir || '').trim();

    function getEffectiveDestDir(isTorrent = false) {
        if (forcedDestDir) return forcedDestDir;
        if (isTorrent) return config.default_dir_torrent || config.default_dir || '/home';
        return config.default_dir || '/home';
    }

    body.innerHTML = `
        <div class="dlm dl-layout-row">
            <div class="dlm-sidebar">
                <div class="dlm-nav active" data-tab="downloads"><i class="fas fa-download"></i> <span>${t('Pobieranie')}</span><span class="dlm-nav-badge" id="dlm-badge-active" style="display:none"></span></div>
                <div class="dlm-nav" data-tab="history"><i class="fas fa-history"></i> <span>${t('Historia')}</span></div>
                <div class="dlm-nav" data-tab="stats"><i class="fas fa-chart-line"></i> <span>${t('Statystyki')}</span></div>
                <div class="dlm-nav" data-tab="settings"><i class="fas fa-cog"></i> <span>${t('Ustawienia')}</span></div>
            </div>
            <div class="dl-main-panel">
            <div class="dlm-content" id="dlm-tab-downloads">
                <div class="dlm-add-bar">
                    <div class="dlm-url-wrap">
                        <textarea class="dlm-url-input" id="dlm-url-input" rows="1" placeholder="${t('Wklej linki (jeden na linię), magnet linki, lub linki do .torrent...')}">${launchOpts?.url ? _dlmEsc(launchOpts.url) : ''}</textarea>
                        <span class="dlm-url-count" id="dlm-url-count"></span>
                    </div>
                    <div class="dlm-add-buttons">
                        <button class="dlm-btn-add" id="dlm-add-btn"><i class="fas fa-plus"></i> ${t('Dodaj')}</button>
                        <button class="dlm-btn-add dlm-btn-paste" id="dlm-paste-btn" title="${t('Wklej ze schowka')}"><i class="fas fa-paste"></i></button>
                        <button class="dlm-btn-add dlm-btn-torrent" id="dlm-torrent-btn" title="${t('Dodaj plik .torrent')}"><i class="fas fa-magnet"></i></button>
                    </div>
                    <input type="file" id="dlm-torrent-file" accept=".torrent" multiple style="display:none">
                </div>
                <div class="dlm-toolbar">
                    <div class="dlm-filter-wrap">
                        <i class="fas fa-search"></i>
                        <input type="text" class="dlm-filter-input" id="dlm-filter" placeholder="${t('Filtruj...')}">
                        <select id="dlm-cat-filter" class="dlm-select-sm dlm-cat-filter-select">
                            <option value="">${t('Wszystkie')}</option>
                        </select>
                    </div>
                    <div class="dlm-bulk-actions">
                        <button class="dlm-btn-sm" id="dlm-pause-all"  title="${t('Wstrzymaj wszystkie aktywne')}"><i class="fas fa-pause"></i> <span>${t('Wstrzymaj')}</span></button>
                        <button class="dlm-btn-sm" id="dlm-resume-all" title="${t('Wznów wszystkie wstrzymane')}"><i class="fas fa-play"></i> <span>${t('Wznów')}</span></button>
                        <button class="dlm-btn-sm" id="dlm-clear" title="${t('Wyczyść zakończone')}"><i class="fas fa-broom"></i> <span>${t('Wyczyść')}</span></button>
                    </div>
                </div>
                <div class="dlm-list" id="dlm-list">
                    <div class="dlm-empty"><i class="fas fa-cloud-download-alt"></i><span>${t('Brak pobierań')}</span><div class="dlm-empty-sub">${t('Wklej link powyżej lub przeciągnij plik .torrent')}</div></div>
                </div>
            </div>
            <div class="dlm-content" id="dlm-tab-history" style="display:none;">
                <div class="dlm-hist-toolbar">
                    <div class="dlm-hist-row">
                        <input type="text" id="dlm-hist-q" placeholder="${t('Szukaj (nazwa, URL)...')}" class="dlm-input-sm">
                        <select id="dlm-hist-status" class="dlm-select-sm">
                            <option value="">${t('Wszystkie statusy')}</option>
                            <option value="completed">${t('Ukończone')}</option>
                            <option value="failed">${t('Błędy')}</option>
                            <option value="cancelled">${t('Anulowane')}</option>
                        </select>
                        <select id="dlm-hist-source" class="dlm-select-sm">
                            <option value="">${t('Wszystkie źródła')}</option>
                            <option value="torrent">${t('Torrent')}</option>
                            <option value="debrid">${t('Debrid')}</option>
                            <option value="direct">${t('Direct')}</option>
                        </select>
                    </div>
                    <div class="dlm-hist-row">
                        <select id="dlm-hist-date" class="dlm-select-sm">
                            <option value="">${t('Cała historia')}</option>
                            <option value="today">${t('Dzisiaj')}</option>
                            <option value="week">${t('Ostatni tydzień')}</option>
                            <option value="month">${t('Ostatni miesiąc')}</option>
                            <option value="range">${t('Zakres dat...')}</option>
                        </select>
                        <span id="dlm-hist-range-wrap" class="dlm-hist-row" style="display:none;">
                            <input type="date" id="dlm-hist-range-from" class="dlm-input-sm dlm-date-input">
                            <span class="dlm-date-separator">–</span>
                            <input type="date" id="dlm-hist-range-to" class="dlm-input-sm dlm-date-input">
                        </span>
                        <button id="dlm-hist-clear-btn" class="dlm-btn-sm dlm-btn-danger"><i class="fas fa-trash"></i> ${t('Wyczyść...')}</button>
                        <div class="dlm-spacer"></div>
                        <div class="dlm-pagination-info" id="dlm-hist-page-info"></div>
                        <button id="dlm-hist-prev" class="dlm-btn-icon" disabled><i class="fas fa-chevron-left"></i></button>
                        <button id="dlm-hist-next" class="dlm-btn-icon" disabled><i class="fas fa-chevron-right"></i></button>
                    </div>
                </div>
                <div class="dlm-list" id="dlm-history-list">
                    <div class="dlm-empty"><i class="fas fa-history"></i><span>${t('Ładowanie historii...')}</span></div>
                </div>
            </div>
            <div class="dlm-content" id="dlm-tab-stats" style="display:none;">
                <div class="dlm-stats-panel">
                    <div class="dlm-stats-header">
                        <div>
                            <div class="dlm-stats-title"><i class="fas fa-chart-bar"></i> ${t('Statystyki')}</div>
                            <div class="dlm-stats-bar-row" id="dlm-stats-bar"></div>
                        </div>
                        <div class="dlm-stats-chip" id="dlm-avg-speed">${t('Śr.:')} —</div>
                    </div>
                    <div class="dlm-stats-grid">
                        <div class="dlm-stat-box">
                            <div class="dlm-stat-label">${t('Dziś')}</div>
                            <div class="dlm-stat-value" id="dlm-bytes-today">—</div>
                        </div>
                        <div class="dlm-stat-box">
                            <div class="dlm-stat-label">${t('7 dni')}</div>
                            <div class="dlm-stat-value" id="dlm-bytes-week">—</div>
                        </div>
                        <div class="dlm-stat-box">
                            <div class="dlm-stat-label">${t('30 dni')}</div>
                            <div class="dlm-stat-value" id="dlm-bytes-month">—</div>
                        </div>
                        <div class="dlm-stat-box">
                            <div class="dlm-stat-label">${t('Łącznie')}</div>
                            <div class="dlm-stat-value" id="dlm-bytes-all">—</div>
                        </div>
                    </div>
                    <div class="dlm-stat-counts">
                        <span class="dlm-stat-pill success" id="dlm-count-completed"><i class="fas fa-check"></i>0</span>
                        <span class="dlm-stat-pill danger" id="dlm-count-failed"><i class="fas fa-times"></i>0</span>
                        <span class="dlm-stat-pill muted" id="dlm-count-cancelled"><i class="fas fa-ban"></i>0</span>
                    </div>
                    <div class="dlm-speed-chart">
                        <div class="dlm-chart-header">
                            <span class="dlm-chart-title"><i class="fas fa-wave-square"></i> ${t('Prędkość (10 min)')}</span>
                            <span id="dlm-speed-current">${t('Aktualnie:')} —</span>
                        </div>
                        <svg id="dlm-speed-chart" viewBox="0 0 180 48" preserveAspectRatio="none"></svg>
                        <div class="dlm-chart-footer">
                            <span id="dlm-speed-avg-sample">${t('Śr. (okno):')} —</span>
                            <span id="dlm-speed-avg-total">${t('Śr. pobierania:')} —</span>
                        </div>
                    </div>
                </div>
            </div>
            <div class="dlm-content" id="dlm-tab-settings" style="display:none;">
                <div class="dlm-settings">
                    <h3 class="dlm-section-title"><i class="fas fa-folder"></i> ${t('Ogólne')}</h3>
                    <div class="dlm-setting-row">
                        <label><i class="fas fa-link dl-icon-label dl-icon-blue"></i> ${t('Folder pobierania HTTP:')}</label>
                        <div class="dlm-path-picker">
                            <input type="text" class="dlm-input" id="dlm-default-dir" value="/home" readonly>
                            <button class="dlm-btn-sm" id="dlm-pick-dir"><i class="fas fa-folder-open"></i></button>
                        </div>
                    </div>
                    <div class="dlm-setting-row">
                        <label><i class="fas fa-magnet dl-icon-label dl-icon-purple"></i> ${t('Folder pobierania torrentów:')}</label>
                        <div class="dlm-path-picker">
                            <input type="text" class="dlm-input" id="dlm-default-dir-torrent" value="/home" readonly>
                            <button class="dlm-btn-sm" id="dlm-pick-dir-torrent"><i class="fas fa-folder-open"></i></button>
                        </div>
                    </div>

                    <h3 class="dlm-section-title dl-section-gap"><i class="fas fa-eye"></i> ${t(t('Folder obserwowany'))}</h3>
                    <p class="dlm-hint">${t('Wrzuć pliki .torrent lub .txt z linkami (jeden URL na linię) do obserwowanego folderu — zostaną automatycznie dodane do kolejki pobierania.')}</p>
                    <div class="dlm-setting-row">
                        <label class="dl-row">
                            <input type="checkbox" id="dlm-watch-enabled">
                            ${t('Włącz folder obserwowany')}
                        </label>
                    </div>
                    <div class="dlm-setting-row">
                        <label><i class="fas fa-binoculars dl-icon-label dl-icon-amber"></i> ${t('Folder obserwowany:')}</label>
                        <div class="dlm-path-picker">
                            <input type="text" class="dlm-input" id="dlm-watch-folder" value="" readonly>
                            <button class="dlm-btn-sm" id="dlm-pick-watch-dir"><i class="fas fa-folder-open"></i></button>
                        </div>
                    </div>

                    <h3 class="dlm-section-title dl-section-gap"><i class="fas fa-file-alt"></i> ${t('Pliki')}</h3>
                    <div class="dlm-setting-row">
                        <label class="dl-row">
                            <input type="checkbox" id="dlm-overwrite-existing">
                            ${t('Nadpisuj istniejące pliki (zamiast tworzyć kopie _1, _2...)')}
                        </label>
                    </div>

                    <div class="dlm-setting-row">
                        <label>${t('Maks. jednoczesnych pobierań:')}</label>
                        <select class="dlm-select" id="dlm-max-concurrent">
                            <option value="1">1</option>
                            <option value="2">2</option>
                            <option value="3" selected>3</option>
                            <option value="5">5</option>
                            <option value="10">10</option>
                        </select>
                    </div>

                    <div class="dlm-setting-row">
                        <label><i class="fas fa-tachometer-alt dl-icon-label dl-icon-amber"></i> ${t('Limit prędkości pobierania:')}</label>
                        <select class="dlm-select" id="dlm-speed-limit">
                            <option value="0">Bez limitu</option>
                            <option value="256">256 KB/s</option>
                            <option value="512">512 KB/s</option>
                            <option value="1024">1 MB/s</option>
                            <option value="2048">2 MB/s</option>
                            <option value="5120">5 MB/s</option>
                            <option value="10240">10 MB/s</option>
                            <option value="20480">20 MB/s</option>
                            <option value="51200">50 MB/s</option>
                            <option value="102400">100 MB/s</option>
                        </select>
                    </div>

                    <h3 class="dlm-section-title dl-section-gap"><i class="fas fa-layer-group"></i> ${t('Kategorie')}</h3>
                    <div class="dlm-setting-row">
                        <label>${t('Auto-sortowanie:')}</label>
                        <label class="dl-checkbox-row">
                            <input type="checkbox" id="dlm-auto-categorize">
                            ${t('Włącz auto-przypisanie do folderów na podstawie rozszerzenia')}
                        </label>
                    </div>
                    <div id="dlm-categories-list" class="dlm-categories-list"></div>
                    <button class="dlm-btn-sm" id="dlm-add-category"><i class="fas fa-plus"></i> ${t('Dodaj nową kategorię')}</button>

                    <h3 class="dlm-section-title dl-section-gap"><i class="fas fa-gem"></i> ${t('Serwis Premium (Debrid)')}</h3>
                    <p class="dlm-hint">${t('Podłącz konto debrid, aby pobierać z hostingów premium (Rapidgator, Uploaded, 1fichier, Mega itp.)')}</p>

                    <div class="dlm-setting-row">
                        <label>${t('Aktywny serwis:')}</label>
                        <select class="dlm-select" id="dlm-debrid-service">
                            <option value="none">${t('Brak (tylko bezpośrednie linki)')}</option>
                            <option value="alldebrid">AllDebrid</option>
                            <option value="realdebrid">Real-Debrid</option>
                            <option value="premiumize">Premiumize.me</option>
                        </select>
                    </div>

                    <div class="dlm-debrid-keys">
                        <div class="dlm-setting-row dlm-key-row" data-service="alldebrid" style="display:none;">
                            <label>${t('Klucz API AllDebrid:')}</label>
                            <div class="dl-key-input-row">
                                <input type="password" class="dlm-input" id="dlm-key-alldebrid" placeholder="Klucz API z alldebrid.com/apikeys">
                                <button class="dlm-btn-sm dlm-test-key" data-service="alldebrid"><i class="fas fa-check-circle"></i> Test</button>
                            </div>
                        </div>
                        <div class="dlm-setting-row dlm-key-row" data-service="realdebrid" style="display:none;">
                            <label>${t('Klucz API Real-Debrid:')}</label>
                            <div class="dl-key-input-row">
                                <input type="password" class="dlm-input" id="dlm-key-realdebrid" placeholder="Klucz API z real-debrid.com/apitoken">
                                <button class="dlm-btn-sm dlm-test-key" data-service="realdebrid"><i class="fas fa-check-circle"></i> Test</button>
                            </div>
                        </div>
                        <div class="dlm-setting-row dlm-key-row" data-service="premiumize" style="display:none;">
                            <label>${t('Klucz API Premiumize:')}</label>
                            <div class="dl-key-input-row">
                                <input type="password" class="dlm-input" id="dlm-key-premiumize" placeholder="Klucz API z premiumize.me/account">
                                <button class="dlm-btn-sm dlm-test-key" data-service="premiumize"><i class="fas fa-check-circle"></i> Test</button>
                            </div>
                        </div>
                        <div class="dlm-setting-row dlm-key-row" data-service="debridlink" style="display:none;">
                            <label>${t('Klucz API Debrid-Link:')}</label>
                            <div class="dl-key-input-row">
                                <input type="password" class="dlm-input" id="dlm-key-debridlink" placeholder="Klucz API z debrid-link.com/webapp/apikey">
                                <button class="dlm-btn-sm dlm-test-key" data-service="debridlink"><i class="fas fa-check-circle"></i> Test</button>
                            </div>
                        </div>
                        <div class="dlm-setting-row dlm-key-row" data-service="torbox" style="display:none;">
                            <label>${t('Klucz API TorBox:')}</label>
                            <div class="dl-key-input-row">
                                <input type="password" class="dlm-input" id="dlm-key-torbox" placeholder="Klucz API z torbox.app/settings">
                                <button class="dlm-btn-sm dlm-test-key" data-service="torbox"><i class="fas fa-check-circle"></i> Test</button>
                            </div>
                        </div>
                    </div>
                    <div class="dlm-debrid-info" id="dlm-debrid-info"></div>

                    <div class="dl-save-wrap">
                        <button class="btn btn-primary" id="dlm-save-config"><i class="fas fa-save"></i> ${t('Zapisz ustawienia')}</button>
                    </div>
                </div>
            </div>
            </div>
        </div>
    `;

    // Tab switching
    const tabPanels = {
        downloads: body.querySelector('#dlm-tab-downloads'),
        history: body.querySelector('#dlm-tab-history'),
        stats: body.querySelector('#dlm-tab-stats'),
        settings: body.querySelector('#dlm-tab-settings'),
    };
    body.querySelectorAll('.dlm-nav').forEach(tab => {
        tab.addEventListener('click', () => {
            body.querySelectorAll('.dlm-nav').forEach(t => t.classList.remove('active'));
            tab.classList.add('active');
            currentTab = tab.dataset.tab;
            Object.entries(tabPanels).forEach(([key, panel]) => {
                if (panel) panel.style.display = currentTab === key ? '' : 'none';
            });
            if (currentTab === 'history') loadHistory();
            if (currentTab === 'stats') loadStats();
        });
    });

    // Debrid service change → show relevant key input
    const debridSelect = body.querySelector('#dlm-debrid-service');
    debridSelect.addEventListener('change', () => {
        const svc = debridSelect.value;
        body.querySelectorAll('.dlm-key-row').forEach(row => {
            row.style.display = row.dataset.service === svc ? 'flex' : 'none';
        });
    });

    // Test debrid key
    body.querySelectorAll('.dlm-test-key').forEach(btn => {
        btn.addEventListener('click', async () => {
            const svc = btn.dataset.service;
            const keyInput = body.querySelector(`#dlm-key-${svc}`);
            const key = keyInput.value.trim();
            if (!key) {
                toast(t('Wpisz klucz API'), 'warning');
                return;
            }
            btn.disabled = true;
            btn.innerHTML = '<i class="fas fa-spinner fa-spin"></i>';
            try {
                let res;
                if (key.includes('***')) {
                    // Key is masked (loaded from server) — test the saved key
                    res = await api('/downloads/test-saved-debrid', {
                        method: 'POST', body: { service: svc }
                    });
                } else {
                    res = await api('/downloads/test-debrid', {
                        method: 'POST', body: { service: svc, api_key: key }
                    });
                }
                const infoEl = body.querySelector('#dlm-debrid-info');
                if (res.ok) {
                    infoEl.innerHTML = `<div class="dlm-info-success"><i class="fas fa-check-circle"></i> ${res.info}</div>`;
                } else {
                    infoEl.innerHTML = `<div class="dlm-info-error"><i class="fas fa-times-circle"></i> ${res.error}</div>`;
                }
            } catch (e) {
                toast(t('Błąd testowania: ') + e.message, 'error');
            } finally {
                btn.disabled = false;
                btn.innerHTML = '<i class="fas fa-check-circle"></i> Test';
            }
        });
    });

    // Save config
    body.querySelector('#dlm-save-config').addEventListener('click', async () => {
        const svc = debridSelect.value;
        const payload = {
            default_dir: body.querySelector('#dlm-default-dir').value,
            default_dir_torrent: body.querySelector('#dlm-default-dir-torrent').value,
            watch_folder: body.querySelector('#dlm-watch-folder').value,
            watch_folder_enabled: body.querySelector('#dlm-watch-enabled').checked,
            overwrite_existing: body.querySelector('#dlm-overwrite-existing').checked,
            max_concurrent: parseInt(body.querySelector('#dlm-max-concurrent').value),
            speed_limit: parseInt(body.querySelector('#dlm-speed-limit').value),
            debrid_service: svc,
            auto_categorize: body.querySelector('#dlm-auto-categorize').checked,
            categories: config.categories,
        };
        // Include API key if changed
        const keyInput = body.querySelector(`#dlm-key-${svc}`);
        if (keyInput && keyInput.value && !keyInput.value.includes('***')) {
            payload[svc + '_api_key'] = keyInput.value.trim();
        }
        const res = await api('/downloads/config', { method: 'PUT', body: payload });
        if (res.ok) toast(t('Ustawienia zapisane'), 'success');
        else toast(res.error || t('Błąd'), 'error');
    });

    // Categories Management
    function renderCategories() {
        const listEl = body.querySelector('#dlm-categories-list');
        listEl.innerHTML = '';
        const cats = config.categories || [];
        
        cats.forEach((cat, index) => {
            const div = document.createElement('div');
            div.className = 'dlm-category-item';
            div.dataset.index = index;
            
            // Path display
            let pathDisplay = _dlmEsc(cat.path || (t('Domyślny folder') + '/' + cat.name));
            
            div.innerHTML = `
                <div class="dlm-cat-header">
                    <span class="dlm-cat-name">${_dlmEsc(cat.name)}</span>
                    <div class="dlm-cat-actions">
                        <button class="dlm-btn-icon dlm-cat-edit" title="${t('Edytuj')}"><i class="fas fa-edit"></i></button>
                        <button class="dlm-btn-icon dlm-cat-del" title="${t('Usuń')}" ${cat.id === 'other' ? 'disabled' : ''}><i class="fas fa-trash-alt"></i></button>
                    </div>
                </div>
                <div class="dlm-cat-path"><i class="fas fa-folder-open"></i> ${pathDisplay}</div>
                <div class="dlm-cat-exts">${_dlmEsc((cat.extensions || []).join(', '))}</div>
                
                <div class="dlm-cat-edit-row" style="display:none;">
                    <div class="dlm-cat-edit-field">
                        <label class="dlm-cat-edit-label">${t('Nazwa kategorii:')}</label>
                        <input type="text" class="dlm-input-sm dlm-cat-name-input dlm-cat-edit-input" value="${_dlmEsc(cat.name)}">
                    </div>
                    <div class="dlm-cat-edit-field">
                        <label class="dlm-cat-edit-label">${t('Folder docelowy (pusty = domyślny):')}</label>
                        <div class="dlm-cat-edit-picker">
                            <input type="text" class="dlm-input-sm dlm-cat-path-input" value="${_dlmEsc(cat.path || '')}">
                            <button class="dlm-btn-sm dlm-pick-cat-path"><i class="fas fa-folder"></i></button>
                        </div>
                    </div>
                    <div class="dlm-cat-edit-field">
                        <label class="dlm-cat-edit-label">${t('Rozszerzenia (oddzielone przecinkami):')}</label>
                        <input type="text" class="dlm-input-sm dlm-cat-exts-input dlm-cat-edit-input" value="${_dlmEsc((cat.extensions || []).join(', '))}">
                    </div>
                    <div class="dlm-cat-edit-actions">
                        <button class="dlm-btn-sm dlm-cat-save"><i class="fas fa-check"></i> OK</button>
                    </div>
                </div>
            `;
            
            const editBtn = div.querySelector('.dlm-cat-edit');
            const editRow = div.querySelector('.dlm-cat-edit-row');
            
            editBtn.addEventListener('click', () => {
                const isHidden = editRow.style.display === 'none';
                editRow.style.display = isHidden ? 'grid' : 'none';
                if(isHidden) div.querySelector('.dlm-cat-name-input').focus();
            });
            
            div.querySelector('.dlm-cat-del').addEventListener('click', () => {
                if(confirm(t('Usunąć kategorię?'))) {
                    config.categories.splice(index, 1);
                    renderCategories();
                    updateCategoryFilter();
                }
            });
            
            div.querySelector('.dlm-pick-cat-path').addEventListener('click', () => {
                openDirPicker(cat.path || config.default_dir || '/home', t('Wybierz folder kategorii'), (path) => {
                    div.querySelector('.dlm-cat-path-input').value = path;
                });
            });
            
            div.querySelector('.dlm-cat-save').addEventListener('click', () => {
                cat.name = div.querySelector('.dlm-cat-name-input').value.trim();
                cat.path = div.querySelector('.dlm-cat-path-input').value.trim();
                const exts = div.querySelector('.dlm-cat-exts-input').value.split(',').map(e => e.trim()).filter(e => e);
                cat.extensions = exts;
                renderCategories();
                updateCategoryFilter();
            });
            
            listEl.appendChild(div);
        });
    }
    
    body.querySelector('#dlm-add-category').addEventListener('click', () => {
        if(!config.categories) config.categories = [];
        config.categories.push({
            id: 'custom_' + Date.now(),
            name: t('Nowa kategoria'),
            path: '',
            extensions: []
        });
        renderCategories();
    });

    function updateCategoryFilter() {
        const sel = body.querySelector('#dlm-cat-filter');
        const current = sel.value;
        sel.innerHTML = '<option value="">' + t('Wszystkie') + '</option>';
        (config.categories || []).forEach(cat => {
            const opt = document.createElement('option');
            opt.value = cat.id;
            opt.textContent = cat.name;
            sel.appendChild(opt);
        });
        sel.value = current;
        if (currentTab === 'downloads') renderDownloads(); 
    }
    
    body.querySelector('#dlm-cat-filter').addEventListener('change', () => {
        renderDownloads();
    });

    // Load config
    async function loadConfig() {
        const res = await api('/downloads/config');
        if (!res.ok) return;
        config = res.config;
        const baseDir = config.default_dir || '/home';
        const torrentDir = config.default_dir_torrent || baseDir;
        body.querySelector('#dlm-default-dir').value = forcedDestDir || baseDir;
        body.querySelector('#dlm-default-dir-torrent').value = forcedDestDir || torrentDir;
        body.querySelector('#dlm-watch-folder').value = config.watch_folder || '';
        body.querySelector('#dlm-watch-enabled').checked = !!config.watch_folder_enabled;
        body.querySelector('#dlm-overwrite-existing').checked = !!config.overwrite_existing;
        body.querySelector('#dlm-max-concurrent').value = config.max_concurrent || 3;
        body.querySelector('#dlm-speed-limit').value = config.speed_limit || 0;
        debridSelect.value = config.debrid_service || 'none';
        // Show matching key row
        body.querySelectorAll('.dlm-key-row').forEach(row => {
            row.style.display = row.dataset.service === config.debrid_service ? 'flex' : 'none';
        });
        // Fill masked keys
        if (config.alldebrid_api_key) body.querySelector('#dlm-key-alldebrid').value = config.alldebrid_api_key;
        if (config.realdebrid_api_key) body.querySelector('#dlm-key-realdebrid').value = config.realdebrid_api_key;
        if (config.premiumize_api_key) body.querySelector('#dlm-key-premiumize').value = config.premiumize_api_key;
        if (config.debridlink_api_key) body.querySelector('#dlm-key-debridlink').value = config.debridlink_api_key;
        if (config.torbox_api_key) body.querySelector('#dlm-key-torbox').value = config.torbox_api_key;
        
        // Categories
        body.querySelector('#dlm-auto-categorize').checked = !!config.auto_categorize;
        renderCategories();
        updateCategoryFilter();
    }

    // ─── Directory picker — uses global openDirPicker() from desktop.js ───

    // Directory picker — HTTP
    body.querySelector('#dlm-pick-dir').addEventListener('click', () => {
        openDirPicker(body.querySelector('#dlm-default-dir').value, t('Folder pobierania HTTP'), path => {
            body.querySelector('#dlm-default-dir').value = path;
        });
    });

    // Directory picker — Torrenty
    body.querySelector('#dlm-pick-dir-torrent').addEventListener('click', () => {
        openDirPicker(body.querySelector('#dlm-default-dir-torrent').value, t('Folder pobierania torrentów'), path => {
            body.querySelector('#dlm-default-dir-torrent').value = path;
        });
    });

    // Directory picker — Watch folder
    body.querySelector('#dlm-pick-watch-dir').addEventListener('click', () => {
        openDirPicker(body.querySelector('#dlm-watch-folder').value || '/home', 'Folder obserwowany', path => {
            body.querySelector('#dlm-watch-folder').value = path;
        });
    });

    // ─── Add download ───
    const urlInput = body.querySelector('#dlm-url-input');
    const addBtn = body.querySelector('#dlm-add-btn');
    const urlCount = body.querySelector('#dlm-url-count');

    // Auto-resize textarea + URL counter
    function _autoResizeUrlInput() {
        urlInput.style.height = 'auto';
        urlInput.style.height = Math.min(urlInput.scrollHeight, 160) + 'px';
        const raw = urlInput.value.trim();
        const n = raw ? raw.split(/[\n\s]+/).filter(u => u.match(/^https?:\/\/|^ftp:\/\/|^magnet:/i)).length : 0;
        urlCount.textContent = n > 1 ? `${n} ${t('linków')}` : '';
        urlCount.style.display = n > 1 ? '' : 'none';
    }
    urlInput.addEventListener('input', _autoResizeUrlInput);
    urlInput.addEventListener('change', _autoResizeUrlInput);

    // Paste from clipboard button
    body.querySelector('#dlm-paste-btn').addEventListener('click', async () => {
        try {
            const text = await navigator.clipboard.readText();
            if (text) {
                const current = urlInput.value.trim();
                urlInput.value = current ? current + '\n' + text : text;
                _autoResizeUrlInput();
                urlInput.focus();
            }
        } catch { toast(t('Brak dostępu do schowka'), 'warning'); }
    });

    async function addDownload() {
        const raw = urlInput.value.trim();
        if (!raw) return;
        // Support multiple URLs (one per line or separated by spaces)
        const urls = raw.split(/[\n\s]+/).filter(u => u.match(/^https?:\/\/|^ftp:\/\/|^magnet:/i));
        if (!urls.length) {
            toast(t('Podaj prawidłowy link (http/https/ftp/magnet)'), 'warning');
            return;
        }
        const hasMagnet = urls.some(u => u.startsWith('magnet:'));

        // Use torrent dir for magnets, HTTP dir for regular links
        let destDir = getEffectiveDestDir(hasMagnet);
        const useDebrid = config.debrid_service && config.debrid_service !== 'none';
        const isMulti = urls.length > 1;

        // Auto-generate package name from common URL parts
        let autoPackageName = '';
        if (isMulti) {
            try {
                const names = urls.map(u => {
                    const parts = new URL(u).pathname.split('/').pop() || '';
                    return parts.replace(/\.[^.]+$/, '').replace(/[._-]+/g, ' ').trim();
                }).filter(Boolean);
                // Find common prefix
                if (names.length > 1) {
                    let prefix = names[0];
                    for (let i = 1; i < names.length; i++) {
                        while (!names[i].toLowerCase().startsWith(prefix.toLowerCase()) && prefix.length > 3) {
                            prefix = prefix.slice(0, -1);
                        }
                    }
                    autoPackageName = prefix.trim().replace(/\s+$/, '') || `${t('Pakiet')} (${urls.length} ${t('plików')})`;
                } else {
                    autoPackageName = `${t('Pakiet')} (${urls.length} ${t('plików')})`;
                }
            } catch { autoPackageName = `${t('Pakiet')} (${urls.length} ${t('plików')})`; }
        }

        // Quick prompt for destination change
        const overlay = document.createElement('div');
        overlay.className = 'modal-overlay';
        overlay.innerHTML = `
            <div class="modal-box" style="width:90%;max-width:520px;">
                <div class="modal-header"><span>${hasMagnet ? '<i class="fas fa-magnet dl-icon-mr"></i>' : ''}${t('Dodaj pobieranie')} (${urls.length} ${urls.length === 1 ? t('link') : t('linków')})</span><button class="modal-close"><i class="fas fa-times"></i></button></div>
                <div class="modal-body">
                    <div class="dlm-setting-row dl-form-row-mb">
                        <label class="dl-label">${t('Folder docelowy:')}</label>
                        <div class="dlm-path-picker">
                            <input type="text" class="dlm-input" id="dlm-add-dest" value="${_dlmEsc(destDir)}" readonly>
                            <button class="dlm-btn-sm" id="dlm-add-pick-dir"><i class="fas fa-folder-open"></i></button>
                        </div>
                    </div>
                    ${useDebrid ? `
                    <label class="dlm-checkbox-label dl-label dl-row">
                        <input type="checkbox" id="dlm-add-debrid" checked> ${t('Użyj serwisu premium')} (${config.debrid_service})
                    </label>` : ''}
                    ${isMulti ? `
                    <div class="dl-section-divider">
                        <h4 class="dl-subsection-title"><i class="fas fa-box dl-icon-mr dl-icon-amber"></i>${t('Pakiet')}</h4>
                        <div class="dlm-setting-row dl-form-row-mb-sm">
                            <label class="dl-label-xs">Nazwa pakietu:</label>
                            <input type="text" class="dlm-input" id="dlm-add-pkg-name" value="${_dlmEsc(autoPackageName)}" placeholder="${t('Nazwa pakietu...')}">
                        </div>
                        <label class="dlm-checkbox-label dl-checkbox-row">
                            <input type="checkbox" id="dlm-add-auto-extract"> <i class="fas fa-file-archive dl-icon-violet"></i> ${t('Autoekstrakcja po zakończeniu (deep extract)')}
                        </label>
                        <div id="dlm-add-extract-opts" class="dl-extract-opts" style="display:none;">
                            <label class="dlm-checkbox-label dl-checkbox-row">
                                <input type="checkbox" id="dlm-add-delete-after"> <i class="fas fa-trash-alt dl-icon-danger"></i> ${t('Usuń archiwa po pomyślnej ekstrakcji')}
                            </label>
                            <div class="dlm-setting-row dl-form-row-mb0">
                                <label class="dl-label-xs"><i class="fas fa-key dl-icon-label dl-icon-amber"></i> ${t('Hasło archiwum (opcjonalne):')}</label>
                                <input type="password" class="dlm-input" id="dlm-add-extract-pw" placeholder="${t('Hasło do rozpakowania...')}">
                            </div>
                        </div>
                    </div>
                    ` : ''}
                    <div class="dl-url-preview">
                        ${urls.map(u => `<div class="dl-url-preview-item">• ${_dlmEsc(u)}</div>`).join('')}
                    </div>
                </div>
                <div class="modal-footer">
                    <button class="btn btn-secondary" id="dlm-add-cancel">${t('Anuluj')}</button>
                    <button class="btn btn-primary" id="dlm-add-confirm"><i class="fas fa-download"></i> ${t('Pobierz')}</button>
                </div>
            </div>
        `;
        document.body.appendChild(overlay);
        const close = () => overlay.remove();
        overlay.querySelector('.modal-close').addEventListener('click', close);
        overlay.querySelector('#dlm-add-cancel').addEventListener('click', close);
        overlay.addEventListener('click', e => { if (e.target === overlay) close(); });

        // Toggle extract options visibility
        const autoExtractCb = overlay.querySelector('#dlm-add-auto-extract');
        if (autoExtractCb) {
            autoExtractCb.addEventListener('change', () => {
                const opts = overlay.querySelector('#dlm-add-extract-opts');
                if (opts) opts.style.display = autoExtractCb.checked ? '' : 'none';
            });
        }

        // Mini dir picker inside add dialog
        overlay.querySelector('#dlm-add-pick-dir')?.addEventListener('click', () => {
            openDirPicker(overlay.querySelector('#dlm-add-dest').value, t('Folder docelowy'), path => {
                overlay.querySelector('#dlm-add-dest').value = path;
            });
        });

        overlay.querySelector('#dlm-add-confirm').addEventListener('click', async () => {
            const dd = overlay.querySelector('#dlm-add-dest').value;
            const ud = overlay.querySelector('#dlm-add-debrid')?.checked ?? true;
            const payload = { urls, dest_dir: dd, use_debrid: ud };

            // Package options
            if (isMulti) {
                payload.package_name = overlay.querySelector('#dlm-add-pkg-name')?.value || '';
                payload.auto_extract = overlay.querySelector('#dlm-add-auto-extract')?.checked || false;
                payload.delete_after_extract = overlay.querySelector('#dlm-add-delete-after')?.checked || false;
                payload.extract_password = overlay.querySelector('#dlm-add-extract-pw')?.value || '';
            }
            close();

            const res = await api('/downloads/add', {
                method: 'POST',
                body: payload
            });
            if (res.ok) {
                toast(`Dodano ${res.added?.length || urls.length} pobieranie(a)`, 'success');
                urlInput.value = '';
                _autoResizeUrlInput();
                loadDownloads();
            } else {
                toast(res.error || t('Błąd dodawania'), 'error');
            }
        });
    }

    addBtn.addEventListener('click', addDownload);
    urlInput.addEventListener('keydown', (e) => {
        // Ctrl+Enter or single-line Enter to submit
        if (e.key === 'Enter' && (e.ctrlKey || e.metaKey || !urlInput.value.includes('\n'))) {
            e.preventDefault();
            addDownload();
        }
    });
    // Drag-and-drop URLs/text into textarea
    urlInput.addEventListener('dragover', (e) => { e.preventDefault(); urlInput.classList.add('dlm-drag-over'); });
    urlInput.addEventListener('dragleave', () => urlInput.classList.remove('dlm-drag-over'));
    urlInput.addEventListener('drop', (e) => {
        e.preventDefault();
        urlInput.classList.remove('dlm-drag-over');
        const text = e.dataTransfer.getData('text');
        if (text) {
            const current = urlInput.value.trim();
            urlInput.value = current ? current + '\n' + text : text;
            _autoResizeUrlInput();
        }
    });

    // ─── Torrent file upload ───
    const torrentBtn = body.querySelector('#dlm-torrent-btn');
    const torrentFile = body.querySelector('#dlm-torrent-file');
    torrentBtn.addEventListener('click', () => torrentFile.click());
    torrentFile.addEventListener('change', async () => {
        const files = Array.from(torrentFile.files);
        if (!files.length) return;
        torrentFile.value = '';

        let destDir = getEffectiveDestDir(true);
        const fileNames = files.map(f => _dlmEsc(f.name)).join(', ');
        const titleText = files.length === 1
            ? `${t('Dodaj torrent:')} ${_dlmEsc(files[0].name)}`
            : `${t('Dodaj')} ${files.length} ${t('torrentów')}`;
        const overlay = document.createElement('div');
        overlay.className = 'modal-overlay';
        overlay.innerHTML = `
            <div class="modal-box" style="width:480px;">
                <div class="modal-header"><span><i class="fas fa-magnet dl-icon-mr"></i>${titleText}</span><button class="modal-close"><i class="fas fa-times"></i></button></div>
                <div class="modal-body">
                    ${files.length > 1 ? `<div class="dl-files-info">${fileNames}</div>` : ''}
                    <div class="dlm-setting-row dl-form-row-mb">
                        <label class="dl-label">${t('Folder docelowy:')}</label>
                        <div class="dlm-path-picker">
                            <input type="text" class="dlm-input" id="dlm-tf-dest" value="${_dlmEsc(destDir)}" readonly>
                            <button class="dlm-btn-sm" id="dlm-tf-pick"><i class="fas fa-folder-open"></i></button>
                        </div>
                    </div>
                </div>
                <div class="modal-footer">
                    <button class="btn btn-secondary" id="dlm-tf-cancel">${t('Anuluj')}</button>
                    <button class="btn btn-primary" id="dlm-tf-ok"><i class="fas fa-download"></i> ${t('Dodaj')}</button>
                </div>
            </div>
        `;
        document.body.appendChild(overlay);
        const close = () => overlay.remove();
        overlay.querySelector('.modal-close').addEventListener('click', close);
        overlay.querySelector('#dlm-tf-cancel').addEventListener('click', close);
        overlay.addEventListener('click', e => { if (e.target === overlay) close(); });

        // Dir picker for torrent
        overlay.querySelector('#dlm-tf-pick')?.addEventListener('click', () => {
            openDirPicker(overlay.querySelector('#dlm-tf-dest').value, 'Folder docelowy', path => {
                overlay.querySelector('#dlm-tf-dest').value = path;
            });
        });

        overlay.querySelector('#dlm-tf-ok').addEventListener('click', async () => {
            const dd = overlay.querySelector('#dlm-tf-dest').value;
            close();

            // Check if any torrents were already processed
            let filesToUpload = files;
            try {
                const checkRes = await api('/downloads/check-processed', {
                    method: 'POST',
                    body: { filenames: files.map(f => f.name) }
                });
                if (checkRes.ok && checkRes.processed?.length) {
                    const names = checkRes.processed.map(n => '• ' + n).join('\n');
                    const confirmed = confirm(
                        `${t('Następujące torrenty były już wcześniej procesowane:')}\n\n${names}\n\n${t('Czy na pewno chcesz je dodać ponownie?')}`
                    );
                    if (!confirmed) {
                        // Remove already-processed from the upload list
                        const processedSet = new Set(checkRes.processed);
                        filesToUpload = files.filter(f => !processedSet.has(f.name));
                        if (!filesToUpload.length) {
                            toast(t('Anulowano — wszystkie torrenty były już procesowane'), 'info');
                            return;
                        }
                    }
                }
            } catch (e) { /* ignore check errors, proceed with upload */ }

            let ok = 0, fail = 0;
            for (const file of filesToUpload) {
                const fd = new FormData();
                fd.append('file', file);
                fd.append('dest_dir', dd);
                try {
                    const resp = await fetch('/api/downloads/add-torrent', {
                        method: 'POST',
                        headers: { 'Authorization': `Bearer ${NAS.token}`, 'X-CSRFToken': NAS.csrfToken },
                        body: fd,
                    });
                    const res = await resp.json();
                    if (res.ok) ok++;
                    else fail++;
                } catch (e) {
                    fail++;
                }
            }
            if (ok) toast(`Dodano ${ok} torrent${ok > 1 ? t('ów') : ''}`, 'success');
            if (fail) toast(`${t('Błąd dodawania')} ${fail} torrent${fail > 1 ? t('ów') : t('a')}`, 'error');
            loadDownloads();
        });
    });

    // Clear
    body.querySelector('#dlm-clear').addEventListener('click', async () => {
        const count = downloads.filter(d => ['completed', 'failed', 'cancelled'].includes(d.status)).length;
        if (!count) { toast(t('Brak elementów do wyczyszczenia'), 'info'); return; }
        const res = await api('/downloads/clear', { method: 'POST' });
        if (res.ok) {
            toast(`${t('Usunięto')} ${res.removed} ${t('elementów')}`, 'info');
            loadDownloads();
        }
    });

    // ─── Filter ───
    const filterInput = body.querySelector('#dlm-filter');
    filterInput.addEventListener('input', () => {
        filterText = filterInput.value.trim().toLowerCase();
        renderDownloads();
    });

    // ─── Bulk actions ───
    body.querySelector('#dlm-pause-all').addEventListener('click', async () => {
        const active = downloads.filter(d => ['downloading', 'pending', 'torrent_downloading'].includes(d.status));
        if (!active.length) { toast(t('Brak aktywnych pobierań'), 'info'); return; }
        let ok = 0;
        for (const d of active) {
            const res = await api('/downloads/pause', { method: 'POST', body: { id: d.id } });
            if (res.ok) ok++;
        }
        toast(`${t('Wstrzymano')} ${ok} ${t('pobierań')}`, 'info');
        loadDownloads();
    });

    body.querySelector('#dlm-resume-all').addEventListener('click', async () => {
        const paused = downloads.filter(d => d.status === 'paused');
        if (!paused.length) { toast(t('Brak wstrzymanych pobierań'), 'info'); return; }
        let ok = 0;
        for (const d of paused) {
            const res = await api('/downloads/resume', { method: 'POST', body: { id: d.id } });
            if (res.ok) ok++;
        }
        toast(`${t('Wznowiono')} ${ok} ${t('pobierań')}`, 'info');
        loadDownloads();
    });

    // ─── History ───
    let historyPage = 1;
    const historyLimit = 20;

    async function loadHistory() {
        const list = body.querySelector('#dlm-history-list');
        const searchInput = body.querySelector('#dlm-hist-q');
        const statusSelect = body.querySelector('#dlm-hist-status');
        const sourceSelect = body.querySelector('#dlm-hist-source');
        const dateSelect = body.querySelector('#dlm-hist-date');
        const rangeWrap = body.querySelector('#dlm-hist-range-wrap');
        const rangeFrom = body.querySelector('#dlm-hist-range-from');
        const rangeTo = body.querySelector('#dlm-hist-range-to');

        if (!list) return;

        // Build query
        const q = searchInput?.value.trim() || '';
        const status = statusSelect?.value || '';
        const source = sourceSelect?.value || '';
        const dateFilter = dateSelect?.value || '';

        // Show/hide custom range inputs
        if (rangeWrap) rangeWrap.style.display = dateFilter === 'range' ? 'flex' : 'none';

        let startTs = '';
        let endTs = '';
        const now = new Date();
        now.setHours(0,0,0,0); // midnight

        if (dateFilter === 'today') {
            startTs = now.getTime();
            const tomorrow = new Date(now);
            tomorrow.setDate(now.getDate() + 1);
            endTs = tomorrow.getTime();
        } else if (dateFilter === 'week') {
            const lastWeek = new Date(now);
            lastWeek.setDate(now.getDate() - 7);
            startTs = lastWeek.getTime();
        } else if (dateFilter === 'month') {
            const lastMonth = new Date(now);
            lastMonth.setMonth(now.getMonth() - 1);
            startTs = lastMonth.getTime();
        } else if (dateFilter === 'range') {
            if (rangeFrom?.value) {
                startTs = new Date(rangeFrom.value).getTime();
            }
            if (rangeTo?.value) {
                const toDate = new Date(rangeTo.value);
                toDate.setDate(toDate.getDate() + 1); // include the end day
                endTs = toDate.getTime();
            }
        }

        list.innerHTML = `<div class="dlm-empty"><i class="fas fa-spinner fa-spin"></i><span>${t('Ładowanie...')}</span></div>`;
        
        const params = new URLSearchParams({
            page: historyPage,
            limit: historyLimit,
            q: q,
            status: status,
            source: source,
            start: startTs ? startTs / 1000 : '',
            end: endTs ? endTs / 1000 : ''
        });

        try {
            const res = await api('/downloads/history?' + params.toString());
            
            if (!res.ok || !res.history?.length) {
                list.innerHTML = '<div class="dlm-empty"><i class="fas fa-history"></i><span>' + t('Brak wyników') + '</span></div>';
                updatePagination(0);
                return;
            }
            
            updatePagination(res.total || 0);

            list.innerHTML = res.history.map(h => {
                const isCompleted = h.event === 'completed';
                const isCancelled = h.event === 'cancelled';
                const icon = isCompleted
                    ? '<i class="fas fa-check-circle dl-icon-success"></i>'
                    : isCancelled
                        ? '<i class="fas fa-ban dl-icon-amber"></i>'
                        : '<i class="fas fa-times-circle dl-icon-danger"></i>';
                const date = new Date(h.timestamp * 1000);
                const dateStr = date.toLocaleString(getLocale(), { day: '2-digit', month: '2-digit', year: 'numeric', hour: '2-digit', minute: '2-digit' });
                const size = h.filesize ? _dlmFormatBytes(h.filesize) : '';
                const duration = h.duration > 0 ? _dlmFormatEta(Math.round(h.duration)) : '';
                const torrentBadge = h.is_torrent ? '<i class="fas fa-magnet dl-icon-torrent-sm"></i>' : '';
                
                // Escape attributes
                const safeUrl = _dlmEsc(h.url).replace(/"/g, '&quot;');
                const safeName = _dlmEsc(h.filename || '').replace(/"/g, '&quot;');
                const safeDest = _dlmEsc(h.dest_dir || '').replace(/"/g, '&quot;');
                const safeUrlCopy = safeUrl.replace(/'/g, "\\'");

                return `
                    <div class="dlm-item dlm-status-${isCompleted ? 'completed' : (isCancelled ? 'cancelled' : 'failed')}">
                        <div class="dlm-item-icon">${icon}</div>
                        <div class="dlm-item-info">
                            <div class="dlm-item-name">${torrentBadge}${_dlmEsc(h.filename || h.url)}</div>
                            <div class="dlm-item-meta">
                                <span>${dateStr}</span>
                                ${size ? `<span>${size}</span>` : ''}
                                ${duration ? `<span>${duration}</span>` : ''}
                                ${h.error ? `<span class="dlm-item-error" title="${_dlmEsc(h.error)}">${_dlmEsc(h.error)}</span>` : ''}
                                ${isCancelled ? `<span class="dlm-item-warn dl-icon-cancelled">${t('Anulowano')}</span>` : ''}
                            </div>
                        </div>
                        <div class="dlm-item-actions">
                            <button class="dlm-btn-icon" title="${t('Kopiuj link')}" onclick="navigator.clipboard.writeText('${safeUrlCopy}');if(typeof toast==='function')toast(typeof t==='function'?t('Skopiowano'):'Skopiowano','info')"><i class="fas fa-copy"></i></button>
                            <button class="dlm-btn-icon dlm-retry-btn" title="${t('Pobierz ponownie')}" data-url="${safeUrl}" data-filename="${safeName}" data-dest="${safeDest}"><i class="fas fa-redo"></i></button>
                        </div>
                    </div>`;
            }).join('');
            
            // Attach retry listeners
            list.querySelectorAll('.dlm-retry-btn').forEach(btn => {
                btn.addEventListener('click', async (e) => {
                    e.stopPropagation();
                    const url = btn.dataset.url;
                    const filename = btn.dataset.filename;
                    const dest = btn.dataset.dest;
                    
                    if (!confirm(`${t('Czy na pewno chcesz pobrać ponownie:')}\n${filename || url}?`)) return;
                    
                    const r = await api('/downloads/history/retry', {
                        method: 'POST',
                        body: JSON.stringify({ url, filename, dest_dir: dest })
                    });
                    
                    if (r.ok) {
                        toast(t('Dodano do pobierania'), 'success');
                    } else {
                        toast(t('Błąd: ') + (r.error || t('Nieznany')), 'error');
                    }
                });
            });

        } catch (e) {
            console.error(e);
            list.innerHTML = '<div class="dlm-empty"><i class="fas fa-exclamation-triangle"></i><span>' + t('Błąd ładowania') + '</span></div>';
        }
    }

    function updatePagination(total) {
        const info = body.querySelector('#dlm-hist-page-info');
        const prev = body.querySelector('#dlm-hist-prev');
        const next = body.querySelector('#dlm-hist-next');
        
        if (!info || !prev || !next) return;
        
        const totalPages = Math.ceil(total / historyLimit) || 1;
        info.textContent = `${t('Strona')} ${historyPage} z ${totalPages} (${total})`;
        
        prev.disabled = historyPage <= 1;
        next.disabled = historyPage >= totalPages;
        
        // Remove old listeners (cloning is a quick hack, or use one-time listeners and re-attach)
        // Here we just re-assign onclick which overrides previous handler
        prev.onclick = () => { if(historyPage > 1) { historyPage--; loadHistory(); } };
        next.onclick = () => { if(historyPage < totalPages) { historyPage++; loadHistory(); } };
    }
    
    // Attach filter listeners
    setTimeout(() => {
        ['#dlm-hist-q', '#dlm-hist-status', '#dlm-hist-source', '#dlm-hist-date', '#dlm-hist-range-from', '#dlm-hist-range-to'].forEach(sel => {
            const el = body.querySelector(sel);
            if (el) {
                // Clear existing listeners not easily possible without removing element, but we can check if already attached
                // Since renderDownloadManager runs once per open, this is okay.
                el.addEventListener('change', () => {
                    historyPage = 1; 
                    loadHistory();
                });
                if (el.tagName === 'INPUT') {
                    el.addEventListener('keyup', (e) => {
                        if (e.key === 'Enter') {
                            historyPage = 1;
                            loadHistory();
                        }
                    });
                }
            }
        });

        // Custom date range inputs
        ['#dlm-hist-range-from', '#dlm-hist-range-to'].forEach(sel => {
            const el = body.querySelector(sel);
            if (el) {
                el.addEventListener('change', () => { historyPage = 1; loadHistory(); });
            }
        });
        
        const clearBtn = body.querySelector('#dlm-hist-clear-btn');
        if (clearBtn) {
            clearBtn.addEventListener('click', async () => {
                // Simple dialog for options
                const days = prompt(t('Wpisz liczbę dni do zachowania (zostaw puste aby wyczyścić wszystko):'), '30');
                if (days === null) return; // cancelled
                
                const olderThan = days.trim() === '' ? null : parseInt(days);
                if (olderThan !== null && isNaN(olderThan)) {
                    toast(t('Nieprawidłowa liczba'), 'error');
                    return;
                }

                const r = await api('/downloads/history/clear', {
                    method: 'POST',
                    body: JSON.stringify({ older_than_days: olderThan })
                });
                if (r.ok) {
                    toast(t('Historia wyczyszczona'), 'success');
                    historyPage = 1;
                    loadHistory();
                } else {
                    toast(t('Błąd czyszczenia'), 'error');
                }
            });
        }
    }, 500);

    // ─── Download list rendering ───
    let packages = {};  // package_id -> package info

    function _renderDlItem(dl) {
        const icon = _dlmStatusIcon(dl.status, dl.is_torrent);
        const statusLabel = _dlmStatusLabel(dl.status, dl);
        const isIndeterminate = dl.progress < 0;
        const progressPct = isIndeterminate ? 0 : (dl.progress || 0);
        const speed = dl.status === 'downloading' ? _dlmFormatSpeed(dl.speed) : '';
        const size = dl.filesize ? _dlmFormatBytes(dl.filesize) : '';
        const downloaded = dl.downloaded ? _dlmFormatBytes(dl.downloaded) : '';
        const isActive = ['downloading', 'resolving', 'torrent_uploading', 'torrent_downloading'].includes(dl.status);
        const isPausable = ['downloading', 'pending', 'torrent_downloading'].includes(dl.status);
        const isCancellable = ['downloading', 'resolving', 'pending', 'paused', 'torrent_uploading', 'torrent_downloading'].includes(dl.status);
        const isMovable = ['pending', 'paused'].includes(dl.status);
        const ftypeClass = _dlmFileTypeClass(dl.filename, dl.is_torrent);

        let sizeInfo = '';
        if (dl.status === 'downloading') {
            sizeInfo = `${downloaded} / ${size || '?'} ${speed ? '— ' + speed : ''}`;
            if (dl.eta > 0) sizeInfo += ` (${_dlmFormatEta(dl.eta)})`;
        } else if (dl.status === 'torrent_downloading') {
            const tSpeed = dl.torrent_speed ? _dlmFormatSpeed(dl.torrent_speed) : '';
            sizeInfo = `${size || '?'} ${tSpeed ? '— ' + tSpeed : ''}`;
            if (dl.torrent_seeders) sizeInfo += ` (${dl.torrent_seeders} seeders)`;
        } else if (dl.status === 'paused') {
            sizeInfo = `${downloaded} / ${size || '?'} — wstrzymano`;
        } else if (dl.status === 'completed') {
            sizeInfo = size || downloaded;
            if (dl.is_torrent && dl.torrent_files_total > 1) sizeInfo += ` (${dl.torrent_files_total} ${t('plików')})`;
        }

        const nameDisplay = dl.is_torrent
            ? `<i class="fas fa-magnet dl-icon-torrent"></i>${_dlmEsc(dl.filename || _dlmShortUrl(dl.url))}`
            : _dlmEsc(dl.filename || _dlmShortUrl(dl.url));

        const cat = (config.categories || []).find(c => c.id === dl.category_id);
        const catBadge = cat ? `<span class="dlm-cat-badge">${_dlmEsc(cat.name)}</span>` : '';

        return `
            <div class="dlm-item dlm-status-${dl.status}${ftypeClass ? ' ' + ftypeClass : ''}${isMovable ? ' dlm-draggable' : ''}" data-id="${dl.id}"${isMovable ? ' draggable="true"' : ''}>
                <div class="dlm-item-icon">${icon}</div>
                <div class="dlm-item-info">
                    <div class="dlm-item-name" title="${_dlmEsc(dl.filename || dl.url)}">${nameDisplay}</div>
                    <div class="dlm-item-meta">
                        ${catBadge}
                        <span class="dlm-item-status">${statusLabel}</span>
                        ${dl.retry_count > 0 ? `<span class="dl-icon-amber" title="${t('Próba')} ${dl.retry_count}"><i class="fas fa-sync-alt"></i> ${dl.retry_count}</span>` : ''}
                        ${sizeInfo ? `<span class="dlm-item-size">${sizeInfo}</span>` : ''}
                        ${dl.dest_path ? `<span class="dlm-item-path" title="${_dlmEsc(dl.dest_path)}">${_dlmEsc(dl.dest_path)}</span>` : ''}
                        ${dl.error ? `<span class="dlm-item-error" title="${_dlmEsc(dl.error)}">${_dlmEsc(dl.error)}</span>` : ''}
                        ${dl.debrid_error ? `<span class="dlm-item-warn" title="${_dlmEsc(dl.debrid_error)}"><i class="fas fa-exclamation-triangle"></i> Debrid: ${_dlmEsc(dl.debrid_error)}</span>` : ''}
                    </div>
                    ${isActive || dl.status === 'paused' ? `
                    <div class="dlm-progress-bar">
                        <div class="dlm-progress-fill${dl.status === 'torrent_downloading' ? ' dlm-progress-torrent' : ''}${dl.status === 'paused' ? ' dlm-progress-paused' : ''}" style="width:${progressPct}%"></div>
                        <span class="dlm-progress-text">${progressPct.toFixed(1)}%</span>
                    </div>` : ''}
                </div>
                <div class="dlm-item-actions">
                    ${isMovable ? `
                        <button class="dlm-btn-icon" data-action="top" title="${t('Przenieś na górę')}"><i class="fas fa-angle-double-up"></i></button>
                    ` : ''}
                    ${isPausable ? `
                        <button class="dlm-btn-icon" data-action="pause" title="${t('Wstrzymaj')}"><i class="fas fa-pause"></i></button>
                    ` : ''}
                    ${dl.status === 'paused' ? `
                        <button class="dlm-btn-icon" data-action="resume" title="${t('Wznów')}"><i class="fas fa-play"></i></button>
                    ` : ''}
                    ${isCancellable ? `
                        <button class="dlm-btn-icon" data-action="cancel" title="${t('Anuluj')}"><i class="fas fa-stop"></i></button>
                    ` : ''}
                    ${dl.status === 'failed' || dl.status === 'cancelled' ? `
                        <button class="dlm-btn-icon" data-action="retry" title="${t('Ponów')}"><i class="fas fa-redo"></i></button>
                    ` : ''}
                    ${dl.status === 'completed' || dl.status === 'failed' || dl.status === 'cancelled' ? `
                        <button class="dlm-btn-icon dlm-btn-danger" data-action="remove" title="${t('Usuń z listy')}"><i class="fas fa-trash"></i></button>
                    ` : ''}
                    ${dl.status === 'completed' && dl.dest_dir ? `
                        <button class="dlm-btn-icon dl-icon-blue" data-action="open-fm" title="${t('Otwórz w menedżerze plików')}"><i class="fas fa-folder-open"></i></button>
                    ` : ''}
                    <button class="dlm-btn-icon" data-action="copy-url" title="Kopiuj link"><i class="fas fa-copy"></i></button>
                    ${dl.error ? `<button class="dlm-btn-icon dl-icon-danger" data-action="copy-error" title="${t('Kopiuj błąd')}"><i class="fas fa-clipboard"></i></button>` : ''}
                </div>
            </div>
        `;
    }

    function _renderPackageHeader(pkg, pkgDownloads) {
        const completedCount = pkgDownloads.filter(d => d.status === 'completed').length;
        const totalCount = pkgDownloads.length;
        const allDone = completedCount === totalCount;
        const totalSize = pkgDownloads.reduce((s, d) => s + (d.filesize || 0), 0);
        const totalDownloaded = pkgDownloads.reduce((s, d) => s + (d.downloaded || 0), 0);
        const overallPct = totalSize > 0 ? (totalDownloaded / totalSize * 100) : (allDone ? 100 : 0);

        const statusClasses = {
            'downloading': 'dl-si-downloading', 'completed': 'dl-si-completed',
            'extracting': 'dl-si-pending', 'extracted': 'dl-si-resolving',
            'extract_failed': 'dl-si-failed',
        };
        const statusLabels = {
            'downloading': `${t('Pobieranie')} (${completedCount}/${totalCount})`,
            'completed': t('Zakończono — gotowe do ekstrakcji'),
            'extracting': t('Wypakowywanie...'),
            'extracted': t('Wypakowano'),
            'extract_failed': t('Błąd ekstrakcji'),
        };
        const statusClass = statusClasses[pkg.status] || '';
        const statusLabel = statusLabels[pkg.status] || pkg.status;

        const showExtract = (pkg.status === 'completed' || pkg.status === 'extract_failed' || pkg.status === 'extracted');
        const isExtracting = pkg.status === 'extracting';

        return `
            <div class="dlm-package" data-pkg-id="${pkg.id}">
                <div class="dlm-package-header" data-pkg-toggle="${pkg.id}">
                    <div class="dlm-package-icon"><i class="fas fa-box dl-pkg-icon"></i></div>
                    <div class="dlm-package-info">
                        <div class="dlm-package-name">
                            <i class="fas fa-caret-down dlm-pkg-caret dl-caret"></i>
                            <strong>${_dlmEsc(pkg.name)}</strong>
                            <span class="dl-pkg-meta">${totalCount} ${t('plików')} • ${_dlmFormatBytes(totalSize)}</span>
                        </div>
                        <div class="dlm-package-status dl-pkg-status">
                            <span class="${statusClass}">${statusLabel}</span>
                            ${pkg.auto_extract ? `<span class="dl-pkg-tag"><i class="fas fa-file-archive"></i> ${t('autoekstrakcja')}</span>` : ''}
                            ${pkg.delete_after_extract ? `<span class="dl-pkg-tag-ml4"><i class="fas fa-trash-alt"></i> ${t('usuń po')}</span>` : ''}
                            ${pkg.extract_password ? `<span class="dl-pkg-tag-ml4"><i class="fas fa-key"></i> ${t('hasło')}</span>` : ''}
                            ${pkg.extract_error ? `<span class="dl-pkg-error" title="${_dlmEsc(pkg.extract_error)}"><i class="fas fa-exclamation-triangle"></i> ${_dlmEsc(pkg.extract_error.substring(0, 80))}</span>` : ''}
                        </div>
                        ${!allDone ? `
                        <div class="dlm-progress-bar dl-progress-mt">
                            <div class="dlm-progress-fill" style="width:${overallPct.toFixed(1)}%"></div>
                            <span class="dlm-progress-text">${overallPct.toFixed(1)}%</span>
                        </div>` : ''}
                    </div>
                    <div class="dlm-package-actions dl-pkg-actions">
                        ${showExtract && !isExtracting ? `
                            <button class="dlm-btn-icon dl-icon-extract" data-pkg-action="extract" data-pkg-id="${pkg.id}" title="Wypakuj (deep extract)"><i class="fas fa-file-archive"></i></button>
                        ` : ''}
                        ${isExtracting ? `
                            <span class="dl-icon-amber"><i class="fas fa-spinner fa-spin"></i></span>
                        ` : ''}
                        ${allDone ? `
                            <button class="dlm-btn-icon dl-icon-blue" data-pkg-action="open-fm" data-pkg-id="${pkg.id}" title="${t('Otwórz w menedżerze plików')}"><i class="fas fa-folder-open"></i></button>
                        ` : ''}
                        <button class="dlm-btn-icon dlm-btn-danger dl-btn-sm-text" data-pkg-action="remove-pkg" data-pkg-id="${pkg.id}" title="${t('Usuń pakiet')}"><i class="fas fa-times"></i></button>
                    </div>
                </div>
                <div class="dlm-package-items" data-pkg-items="${pkg.id}" style="display:none">
                    ${pkgDownloads.map(dl => _renderDlItem(dl)).join('')}
                </div>
            </div>
        `;
    }

    function _matchesFilter(dl) {
        const catFilter = body.querySelector('#dlm-cat-filter')?.value;
        if (catFilter && dl.category_id !== catFilter) return false;

        if (!filterText) return true;
        const hay = ((dl.filename || '') + ' ' + (dl.url || '') + ' ' + (dl.dest_path || '') + ' ' + (dl.status || '')).toLowerCase();
        return hay.includes(filterText);
    }

    function renderDownloads() {
        const list = body.querySelector('#dlm-list');
        const filtered = downloads.filter(_matchesFilter);

        // Update sidebar badge
        const activeCount = downloads.filter(d => ['downloading','resolving','torrent_downloading','torrent_uploading','pending'].includes(d.status)).length;
        const badge = body.querySelector('#dlm-badge-active');
        if (badge) {
            if (activeCount > 0) { badge.textContent = activeCount; badge.style.display = ''; }
            else { badge.style.display = 'none'; }
        }

        if (!filtered.length) {
            list.innerHTML = filterText
                ? `<div class="dlm-empty"><i class="fas fa-search"></i><span>${t('Brak wyników')}</span><div class="dlm-empty-sub">${t('Spróbuj zmienić filtr lub kategorię')}</div></div>`
                : `<div class="dlm-empty"><i class="fas fa-cloud-download-alt"></i><span>${t('Brak pobierań')}</span><div class="dlm-empty-sub">${t('Wklej link powyżej lub przeciągnij plik .torrent')}</div></div>`;
            return;
        }

        // Group downloads: packages vs orphans
        const pkgMap = {};  // pkg_id -> [dl, ...]
        const orphans = [];
        const pkgOrder = [];  // preserve ordering

        for (const dl of filtered) {
            if (dl.package_id && packages[dl.package_id]) {
                if (!pkgMap[dl.package_id]) {
                    pkgMap[dl.package_id] = [];
                    pkgOrder.push(dl.package_id);
                }
                pkgMap[dl.package_id].push(dl);
            } else {
                orphans.push(dl);
            }
        }

        let html = '';

        // Render packages first (active ones prioritized)
        for (const pkgId of pkgOrder) {
            const pkg = packages[pkgId];
            const pkgDls = pkgMap[pkgId];
            if (pkg && pkgDls?.length) {
                html += _renderPackageHeader(pkg, pkgDls);
            }
        }

        // Render orphan (non-package) downloads
        for (const dl of orphans) {
            html += _renderDlItem(dl);
        }

        list.innerHTML = html;

        // Bind package toggle (collapse/expand)
        list.querySelectorAll('[data-pkg-toggle]').forEach(hdr => {
            hdr.addEventListener('click', (e) => {
                if (e.target.closest('[data-pkg-action]')) return;
                const pkgId = hdr.dataset.pkgToggle;
                const items = list.querySelector(`[data-pkg-items="${pkgId}"]`);
                const caret = hdr.querySelector('.dlm-pkg-caret');
                if (items) {
                    const hidden = items.style.display === 'none';
                    items.style.display = hidden ? '' : 'none';
                    if (caret) caret.style.transform = hidden ? '' : 'rotate(-90deg)';
                }
            });
        });

        // Bind package actions
        list.querySelectorAll('[data-pkg-action]').forEach(btn => {
            btn.addEventListener('click', async (e) => {
                e.stopPropagation();
                const pkgId = btn.dataset.pkgId;
                const action = btn.dataset.pkgAction;

                if (action === 'extract') {
                    _showExtractDialog(pkgId);
                } else if (action === 'open-fm') {
                    const pkg = packages[pkgId];
                    if (pkg?.dest_dir) {
                        const app = NAS.apps?.find(a => a.id === 'file-manager');
                        if (app) openApp(app, { path: pkg.dest_dir });
                    }
                } else if (action === 'remove-pkg') {
                    await api('/downloads/package/remove', { method: 'POST', body: { package_id: pkgId } });
                    delete packages[pkgId];
                    renderDownloads();
                }
            });
        });

        // Bind download item actions
        list.querySelectorAll('[data-action]').forEach(btn => {
            btn.addEventListener('click', async () => {
                const id = btn.closest('.dlm-item').dataset.id;
                const action = btn.dataset.action;
                if (action === 'copy-url') {
                    const dl = downloads.find(d => d.id === id);
                    if (dl?.url) { navigator.clipboard.writeText(dl.url); toast(t('Skopiowano link'), 'info'); }
                    return;
                }
                if (action === 'copy-error') {
                    const dl = downloads.find(d => d.id === id);
                    if (dl?.error) { navigator.clipboard.writeText(dl.error); toast(t('Skopiowano błąd'), 'info'); }
                    return;
                }
                if (action === 'cancel') {
                    await api('/downloads/cancel', { method: 'POST', body: { id } });
                } else if (action === 'retry') {
                    await api('/downloads/retry', { method: 'POST', body: { id } });
                } else if (action === 'remove') {
                    await api('/downloads/remove', { method: 'POST', body: { id } });
                } else if (action === 'pause') {
                    await api('/downloads/pause', { method: 'POST', body: { id } });
                } else if (action === 'resume') {
                    await api('/downloads/resume', { method: 'POST', body: { id } });
                } else if (action === 'top') {
                    await api('/downloads/reorder', { method: 'POST', body: { id, direction: 'top' } });
                } else if (action === 'open-fm') {
                    const dl = downloads.find(d => d.id === id);
                    if (dl) {
                        const fmPath = dl.dest_dir || '/';
                        const app = NAS.apps?.find(a => a.id === 'file-manager');
                        if (app) openApp(app, { path: fmPath });
                    }
                    return;
                }
                loadDownloads();
            });
        });
    }

    // ─── Extract dialog ───
    function _showExtractDialog(packageId) {
        const pkg = packages[packageId];
        if (!pkg) return;

        const overlay = document.createElement('div');
        overlay.className = 'modal-overlay';
        overlay.innerHTML = `
            <div class="modal-box" style="width:460px;">
                <div class="modal-header"><span><i class="fas fa-file-archive dl-icon-mr dl-icon-violet"></i>Deep Extract: ${_dlmEsc(pkg.name)}</span><button class="modal-close"><i class="fas fa-times"></i></button></div>
                <div class="modal-body">
                    <p class="dl-hint-text">${t('Rekurencyjna ekstrakcja wszystkich archiwów w folderze pakietu. Archiwa w archiwach również zostaną rozpakowane.')}</p>
                    <div class="dlm-setting-row dl-form-row-mb10">
                        <label class="dl-label"><i class="fas fa-folder dl-icon-label dl-icon-blue"></i> ${t('Folder:')}</label>
                        <span class="dl-path-text">${_dlmEsc(pkg.dest_dir)}</span>
                    </div>
                    <label class="dlm-checkbox-label dl-checkbox-row-lg">
                        <input type="checkbox" id="dlm-ext-delete" ${pkg.delete_after_extract ? 'checked' : ''}> <i class="fas fa-trash-alt dl-icon-danger"></i> ${t('Usuń archiwa po pomyślnej ekstrakcji')}
                    </label>
                    <div class="dlm-setting-row dl-form-row-mb0">
                        <label class="dl-label"><i class="fas fa-key dl-icon-label dl-icon-amber"></i> ${t('Hasło (opcjonalne):')}</label>
                        <input type="password" class="dlm-input" id="dlm-ext-pw" value="" placeholder="${t('Hasło do archiwum...')}">
                    </div>
                    ${pkg.extract_error ? `<div class="dl-error-box"><i class="fas fa-exclamation-triangle"></i> ${_dlmEsc(pkg.extract_error)}</div>` : ''}
                </div>
                <div class="modal-footer">
                    <button class="btn btn-secondary" id="dlm-ext-cancel">${t('Anuluj')}</button>
                    <button class="btn btn-primary dl-btn-violet" id="dlm-ext-ok"><i class="fas fa-file-archive"></i> Wypakuj</button>
                </div>
            </div>
        `;
        document.body.appendChild(overlay);
        const close = () => overlay.remove();
        overlay.querySelector('.modal-close').addEventListener('click', close);
        overlay.querySelector('#dlm-ext-cancel').addEventListener('click', close);
        overlay.addEventListener('click', e => { if (e.target === overlay) close(); });

        overlay.querySelector('#dlm-ext-ok').addEventListener('click', async () => {
            const pw = overlay.querySelector('#dlm-ext-pw').value;
            const del = overlay.querySelector('#dlm-ext-delete').checked;
            close();
            const res = await api('/downloads/extract', {
                method: 'POST',
                body: { package_id: packageId, password: pw, delete_after: del }
            });
            if (res.ok) {
                toast(t('Ekstrakcja rozpoczęta'), 'info');
                loadDownloads();
            } else {
                toast(res.error || t('Błąd ekstrakcji'), 'error');
            }
        });
    }

    // ─── Drag & Drop Reorder ───
    function _initDragAndDrop() {
        const list = body.querySelector('#dlm-list');
        if (!list) return;

        let draggedItem = null;

        list.addEventListener('dragstart', (e) => {
            const item = e.target.closest('.dlm-draggable');
            if (!item) return;
            draggedItem = item;
            e.dataTransfer.effectAllowed = 'move';
            e.dataTransfer.setData('text/plain', item.dataset.id);
            setTimeout(() => item.classList.add('dlm-dragging'), 0);
        });

        list.addEventListener('dragend', (e) => {
            const item = e.target.closest('.dlm-draggable');
            if (item) item.classList.remove('dlm-dragging');
            draggedItem = null;
            list.querySelectorAll('.dlm-drag-over-top, .dlm-drag-over-bottom').forEach(el => {
                el.classList.remove('dlm-drag-over-top', 'dlm-drag-over-bottom');
            });
        });

        list.addEventListener('dragover', (e) => {
            e.preventDefault();
            const item = e.target.closest('.dlm-draggable');
            
            // Cleanup others
            list.querySelectorAll('.dlm-drag-over-top, .dlm-drag-over-bottom').forEach(el => {
                if (el !== item) el.classList.remove('dlm-drag-over-top', 'dlm-drag-over-bottom');
            });

            if (item && item !== draggedItem) {
                const rect = item.getBoundingClientRect();
                const offset = e.clientY - rect.top;
                if (offset > rect.height / 2) {
                    item.classList.remove('dlm-drag-over-top');
                    item.classList.add('dlm-drag-over-bottom');
                } else {
                    item.classList.remove('dlm-drag-over-bottom');
                    item.classList.add('dlm-drag-over-top');
                }
            }
        });

        list.addEventListener('drop', async (e) => {
            e.preventDefault();
            const target = e.target.closest('.dlm-draggable');
            const isBottom = target && target.classList.contains('dlm-drag-over-bottom');

            list.querySelectorAll('.dlm-drag-over-top, .dlm-drag-over-bottom').forEach(el => {
                el.classList.remove('dlm-drag-over-top', 'dlm-drag-over-bottom');
            });

            if (draggedItem) {
                if (target && draggedItem !== target) {
                    // Move in DOM (optimistic)
                    if (isBottom) {
                        list.insertBefore(draggedItem, target.nextSibling);
                    } else {
                        list.insertBefore(draggedItem, target);
                    }
                } else if (!target && e.target.closest('.dlm-list') === list) {
                     // Dropped on empty space/end of list
                     list.appendChild(draggedItem);
                }
                
                // Collect IDs
                const movableIds = Array.from(list.querySelectorAll('.dlm-draggable')).map(el => el.dataset.id);
                
                // Send to backend
                await api('/downloads/reorder', {
                    method: 'POST', 
                    body: { ordered_ids: movableIds }
                });
                loadDownloads();
            }
        });
    }
    _initDragAndDrop();

    async function loadDownloads() {
        const res = await api('/downloads/list');
        if (res.ok) {
            downloads = res.items || [];
            // Build packages map
            packages = {};
            if (res.packages) {
                for (const p of res.packages) {
                    packages[p.id] = p;
                }
            }
            renderDownloads();
            _updateStatsBar();
        }
    }

    function _aggregateSpeed() {
        return downloads.reduce((sum, d) => {
            if (d.status === 'downloading') return sum + (d.speed || 0);
            if (d.status === 'torrent_downloading') return sum + (d.torrent_speed || 0);
            return sum;
        }, 0);
    }

    function _updateStatsBar() {
        const bar = body.querySelector('#dlm-stats-bar');
        if (!bar) return;
        const active = downloads.filter(d => ['downloading', 'resolving', 'torrent_downloading', 'torrent_uploading'].includes(d.status)).length;
        const pending = downloads.filter(d => d.status === 'pending').length;
        const completed = downloads.filter(d => d.status === 'completed').length;
        const speed = _aggregateSpeed();
        let parts = [];
        if (active) parts.push(`<span class="dl-stat-active"><i class="fas fa-arrow-down"></i> ${active}</span>`);
        if (pending) parts.push(`<span class="dl-stat-pending"><i class="fas fa-clock"></i> ${pending}</span>`);
        if (completed) parts.push(`<span class="dl-stat-completed"><i class="fas fa-check"></i> ${completed}</span>`);
        if (speed > 0) parts.push(`<span class="dl-stat-speed">${_dlmFormatSpeed(speed)}</span>`);
        bar.innerHTML = parts.join(' ');
        _recordSpeedSample(speed);
        _updateSpeedLegend(speed, _calcSampleAverage(), metricsCache?.average_speed || 0);
    }

    function _calcSampleAverage() {
        if (!speedSamples.length) return 0;
        return speedSamples.reduce((sum, p) => sum + (p.v || 0), 0) / speedSamples.length;
    }

    function _recordSpeedSample(currentSpeed = null) {
        const now = Date.now();
        const speed = currentSpeed !== null ? currentSpeed : _aggregateSpeed();
        if (!speedSamples.length || now - speedSamples[speedSamples.length - 1].t >= 3000) {
            speedSamples.push({ t: now, v: speed });
        } else {
            speedSamples[speedSamples.length - 1] = { t: now, v: speed };
        }
        const cutoff = now - SPEED_WINDOW_MS;
        while (speedSamples.length && speedSamples[0].t < cutoff) speedSamples.shift();
        _renderSpeedChart();
    }

    function _renderSpeedChart() {
        const svg = body.querySelector('#dlm-speed-chart');
        if (!svg) return;
        const width = 180;
        const height = 48;
        svg.setAttribute('viewBox', `0 0 ${width} ${height}`);
        if (!speedSamples.length) {
            svg.innerHTML = `<text x="6" y="${height / 2}" fill="var(--text-muted)">brak danych</text>`;
            _updateSpeedLegend(_aggregateSpeed(), 0, metricsCache?.average_speed || 0);
            return;
        }
        const samples = speedSamples.slice();
        const minT = samples[0].t;
        const maxT = samples[samples.length - 1].t || minT + 1;
        const span = Math.max(maxT - minT, 1);
        const maxV = Math.max(...samples.map(s => s.v), 1);
        const points = samples.map(s => {
            const x = ((s.t - minT) / span) * width;
            const y = height - ((s.v / maxV) * (height - 6)) - 3;
            return `${x.toFixed(2)},${Math.max(0, y).toFixed(2)}`;
        }).join(' ');
        svg.innerHTML = `
            <polyline points="${points}" fill="none" stroke="var(--success)" stroke-width="1.5" stroke-linejoin="round" stroke-linecap="round"></polyline>
            <line x1="0" y1="${height - 1}" x2="${width}" y2="${height - 1}" stroke="var(--border)" stroke-width="0.5"></line>
        `;
        const currentVal = samples[samples.length - 1].v || 0;
        _updateSpeedLegend(currentVal, _calcSampleAverage(), metricsCache?.average_speed || 0);
    }

    function _updateSpeedLegend(current, sampleAvg, overallAvg) {
        const curEl = body.querySelector('#dlm-speed-current');
        if (curEl) curEl.textContent = `Aktualnie: ${current > 0 ? _dlmFormatSpeed(current) : '—'}`;
        const sampleEl = body.querySelector('#dlm-speed-avg-sample');
        if (sampleEl) sampleEl.textContent = `${t('Śr. (okno):')} ${sampleAvg > 0 ? _dlmFormatSpeed(sampleAvg) : '—'}`;
        const overallEl = body.querySelector('#dlm-speed-avg-total');
        if (overallEl) overallEl.textContent = overallAvg > 0 ? _dlmFormatSpeed(overallAvg) : '—';
    }

    function _renderStatsPanel(metrics = {}) {
        metricsCache = metrics || {};
        const bytes = metricsCache.bytes || {};
        const counts = metricsCache.counts || {};
        const avgSpeed = metricsCache.average_speed || 0;
        const setText = (sel, val) => {
            const el = body.querySelector(sel);
            if (el) el.textContent = val;
        };
        setText('#dlm-bytes-today', bytes.today ? _dlmFormatBytes(bytes.today) : '—');
        setText('#dlm-bytes-week', bytes.week ? _dlmFormatBytes(bytes.week) : '—');
        setText('#dlm-bytes-month', bytes.month ? _dlmFormatBytes(bytes.month) : '—');
        setText('#dlm-bytes-all', bytes.all_time ? _dlmFormatBytes(bytes.all_time) : '—');
        setText('#dlm-avg-speed', `${t('Śr.:')} ${avgSpeed ? _dlmFormatSpeed(avgSpeed) : '—'}`);
        const completedEl = body.querySelector('#dlm-count-completed');
        if (completedEl) completedEl.innerHTML = `<i class="fas fa-check"></i>${counts.completed || 0}`;
        const failedEl = body.querySelector('#dlm-count-failed');
        if (failedEl) failedEl.innerHTML = `<i class="fas fa-times"></i>${counts.failed || 0}`;
        const cancelledEl = body.querySelector('#dlm-count-cancelled');
        if (cancelledEl) cancelledEl.innerHTML = `<i class="fas fa-ban"></i>${counts.cancelled || 0}`;
        _updateSpeedLegend(_aggregateSpeed(), _calcSampleAverage(), avgSpeed || 0);
        _renderSpeedChart();
    }

    async function loadStats() {
        const res = await api('/downloads/stats');
        if (res.ok) {
            _renderStatsPanel(res.metrics || {});
        }
    }

    function _startTimers() {
        if (!statsTimer) statsTimer = setInterval(loadStats, 20000);
        if (!speedSampleTimer) speedSampleTimer = setInterval(() => _recordSpeedSample(), SPEED_SAMPLE_MS);
    }

    function _stopTimers() {
        if (statsTimer) { clearInterval(statsTimer); statsTimer = null; }
        if (speedSampleTimer) { clearInterval(speedSampleTimer); speedSampleTimer = null; }
    }

    // ─── Socket.IO real-time updates ───
    function _updateItemInPlace(data) {
        const list = body.querySelector('#dlm-list');
        if (!list) return false;
        const el = list.querySelector(`.dlm-item[data-id="${data.id}"]`);
        if (!el) return false;

        // Update status class (preserve touch-swipe state)
        const wasSwiped = el.classList.contains('dlm-swiped');
        el.className = `dlm-item dlm-status-${data.status}`;
        if (wasSwiped) el.classList.add('dlm-swiped');

        // Create temp container to parse new HTML
        const tmp = document.createElement('div');
        tmp.innerHTML = _renderDlItem(data);
        const newEl = tmp.firstElementChild;
        if (!newEl) return false;

        // Update icon
        const oldIcon = el.querySelector('.dlm-item-icon');
        const newIcon = newEl.querySelector('.dlm-item-icon');
        if (oldIcon && newIcon && oldIcon.innerHTML !== newIcon.innerHTML) oldIcon.innerHTML = newIcon.innerHTML;

        // Update meta (status text, size, speed, error, path)
        const oldMeta = el.querySelector('.dlm-item-meta');
        const newMeta = newEl.querySelector('.dlm-item-meta');
        if (oldMeta && newMeta) oldMeta.innerHTML = newMeta.innerHTML;

        // Update name
        const oldName = el.querySelector('.dlm-item-name');
        const newName = newEl.querySelector('.dlm-item-name');
        if (oldName && newName && oldName.innerHTML !== newName.innerHTML) oldName.innerHTML = newName.innerHTML;

        // Update progress bar
        const oldProg = el.querySelector('.dlm-progress-bar');
        const newProg = newEl.querySelector('.dlm-progress-bar');
        if (newProg) {
            if (oldProg) {
                // Just update width + text for smooth animation
                const fill = oldProg.querySelector('.dlm-progress-fill');
                const newFill = newProg.querySelector('.dlm-progress-fill');
                if (fill && newFill) {
                    fill.style.width = newFill.style.width;
                    fill.className = newFill.className;
                }
                const text = oldProg.querySelector('.dlm-progress-text');
                const newText = newProg.querySelector('.dlm-progress-text');
                if (text && newText) text.textContent = newText.textContent;
            } else {
                // Add progress bar (status changed to active)
                const info = el.querySelector('.dlm-item-info');
                if (info) info.appendChild(newProg);
            }
        } else if (oldProg) {
            // Remove progress bar (completed/failed)
            oldProg.remove();
        }

        // Update actions only if status category changed
        const oldActions = el.querySelector('.dlm-item-actions');
        const newActions = newEl.querySelector('.dlm-item-actions');
        if (oldActions && newActions && oldActions.innerHTML !== newActions.innerHTML) {
            oldActions.innerHTML = newActions.innerHTML;
            // Rebind action buttons for this item
            oldActions.querySelectorAll('[data-action]').forEach(btn => {
                btn.addEventListener('click', async () => {
                    const id = data.id;
                    const action = btn.dataset.action;
                    if (action === 'copy-url') {
                        const dl = downloads.find(d => d.id === id);
                        if (dl?.url) { navigator.clipboard.writeText(dl.url); toast('Skopiowano link', 'info'); }
                        return;
                    }
                    if (action === 'copy-error') {
                        const dl = downloads.find(d => d.id === id);
                        if (dl?.error) { navigator.clipboard.writeText(dl.error); toast(t('Skopiowano błąd'), 'info'); }
                        return;
                    }
                    if (action === 'cancel') await api('/downloads/cancel', { method: 'POST', body: { id } });
                    else if (action === 'retry') await api('/downloads/retry', { method: 'POST', body: { id } });
                    else if (action === 'remove') await api('/downloads/remove', { method: 'POST', body: { id } });
                    else if (action === 'pause') await api('/downloads/pause', { method: 'POST', body: { id } });
                    else if (action === 'resume') await api('/downloads/resume', { method: 'POST', body: { id } });
                    else if (action === 'top') await api('/downloads/reorder', { method: 'POST', body: { id, direction: 'top' } });
                    else if (action === 'open-fm') {
                        const dl = downloads.find(d => d.id === id);
                        if (dl) {
                            const app = NAS.apps?.find(a => a.id === 'file-manager');
                            if (app) openApp(app, { path: dl.dest_dir || '/' });
                        }
                        return;
                    }
                    loadDownloads();
                });
            });
        }
        return true;
    }

    function _updatePackageInPlace(data) {
        const list = body.querySelector('#dlm-list');
        if (!list) return false;
        const pkgEl = list.querySelector(`.dlm-package[data-pkg-id="${data.id}"]`);
        if (!pkgEl) return false;

        const pkg = data;
        const pkgDls = downloads.filter(d => d.package_id === pkg.id);
        if (!pkgDls.length) return false;

        // Recreate just the header status parts
        const tmp = document.createElement('div');
        tmp.innerHTML = _renderPackageHeader(pkg, pkgDls);
        const newPkg = tmp.firstElementChild;
        if (!newPkg) return false;

        // Update package status line
        const oldStatus = pkgEl.querySelector('.dlm-package-status');
        const newStatus = newPkg.querySelector('.dlm-package-status');
        if (oldStatus && newStatus) oldStatus.innerHTML = newStatus.innerHTML;

        // Update package progress bar
        const oldBar = pkgEl.querySelector('.dlm-package-header > .dlm-package-info > .dlm-progress-bar');
        const newBar = newPkg.querySelector('.dlm-package-header > .dlm-package-info > .dlm-progress-bar');
        if (newBar) {
            if (oldBar) {
                const fill = oldBar.querySelector('.dlm-progress-fill');
                const newFill = newBar.querySelector('.dlm-progress-fill');
                if (fill && newFill) fill.style.width = newFill.style.width;
                const text = oldBar.querySelector('.dlm-progress-text');
                const newText = newBar.querySelector('.dlm-progress-text');
                if (text && newText) text.textContent = newText.textContent;
            } else {
                pkgEl.querySelector('.dlm-package-info')?.appendChild(newBar);
            }
        } else if (oldBar) {
            oldBar.remove();
        }

        // Update package count text in name
        const oldName = pkgEl.querySelector('.dlm-package-name');
        const newName = newPkg.querySelector('.dlm-package-name');
        if (oldName && newName && oldName.innerHTML !== newName.innerHTML) oldName.innerHTML = newName.innerHTML;

        // Update actions
        const oldActs = pkgEl.querySelector('.dlm-package-actions');
        const newActs = newPkg.querySelector('.dlm-package-actions');
        if (oldActs && newActs && oldActs.innerHTML !== newActs.innerHTML) {
            oldActs.innerHTML = newActs.innerHTML;
            oldActs.querySelectorAll('[data-pkg-action]').forEach(btn => {
                btn.addEventListener('click', async (e) => {
                    e.stopPropagation();
                    const pkgId = btn.dataset.pkgId;
                    const action = btn.dataset.pkgAction;
                    if (action === 'extract') _showExtractDialog(pkgId);
                    else if (action === 'open-fm') {
                        const p = packages[pkgId];
                        if (p?.dest_dir) {
                            const app = NAS.apps?.find(a => a.id === 'file-manager');
                            if (app) openApp(app, { path: p.dest_dir });
                        }
                    } else if (action === 'remove-pkg') {
                        await api('/downloads/package/remove', { method: 'POST', body: { package_id: pkgId } });
                        delete packages[pkgId];
                        renderDownloads();
                    }
                });
            });
        }
        return true;
    }

    function onDlUpdate(data) {
        const idx = downloads.findIndex(d => d.id === data.id);
        if (idx >= 0) {
            downloads[idx] = data;
        } else {
            downloads.unshift(data);
            renderDownloads(); // new item — need full render
            _updateStatsBar();
            return;
        }
        // Try incremental update first
        if (!_updateItemInPlace(data)) {
            renderDownloads(); // fallback to full render
            _updateStatsBar();
            return;
        }
        // Also update parent package header if applicable
        if (data.package_id && packages[data.package_id]) {
            _updatePackageInPlace(packages[data.package_id]);
        }
        _updateStatsBar();
        if (['completed', 'failed', 'cancelled'].includes(data.status)) {
            loadStats();
        }
    }

    function onDlRemoved(data) {
        downloads = downloads.filter(d => d.id !== data.id);
        renderDownloads();
        _updateStatsBar();
    }

    function onPkgUpdate(data) {
        if (data?.id) {
            packages[data.id] = data;
            if (!_updatePackageInPlace(data)) {
                renderDownloads();
            }
        }
    }

    function onPkgRemoved(data) {
        if (data?.id) {
            delete packages[data.id];
            renderDownloads();
        }
    }

    function onDlCompleted(data) {
        if (!data?.filename) return;
        const folderPath = (data.folder || data.dest_dir || data.dest_path || '').trim();
        const message = `Pobrano: ${data.filename}`;
        if (folderPath) {
            toastWithAction(
                message,
                'success',
                t('Otwórz folder'),
                () => {
                    const app = NAS.apps?.find(a => a.id === 'file-manager');
                    if (app) openApp(app, { path: folderPath });
                }
            );
        } else {
            toast(message, 'success');
        }
    }

    if (NAS.socket) {
        NAS.socket.on('dl:update', onDlUpdate);
        NAS.socket.on('dl:removed', onDlRemoved);
        NAS.socket.on('dl:package_update', onPkgUpdate);
        NAS.socket.on('dl:package_removed', onPkgRemoved);
        NAS.socket.on('dl:completed', onDlCompleted);
    }

    // Cleanup on window close
    const winData = WM.windows.get('download-manager');
    if (winData) {
        const origClose = winData.onClose;
        winData.onClose = () => {
            if (NAS.socket) {
                NAS.socket.off('dl:update', onDlUpdate);
                NAS.socket.off('dl:removed', onDlRemoved);
                NAS.socket.off('dl:package_update', onPkgUpdate);
                NAS.socket.off('dl:package_removed', onPkgRemoved);
                NAS.socket.off('dl:completed', onDlCompleted);
            }
            _stopTimers();
            if (origClose) origClose();
        };
    }

    // ─── Init ───
    loadConfig();
    loadDownloads();
    loadStats();
    _recordSpeedSample();
    // ─── Touch swipe support ───
    function _setupSwipe() {
        const list = body.querySelector('#dlm-list');
        if (!list) return;
        let startX = 0, current = null;
        list.addEventListener('touchstart', e => {
            const item = e.target.closest('.dlm-item');
            if(item && !e.target.closest('.dlm-item-actions')) {
                current = item;
                startX = e.touches[0].clientX;
            }
        }, {passive: true});
        list.addEventListener('touchend', e => {
            if (!current) return;
            const diff = startX - e.changedTouches[0].clientX;
            if (diff > 50) current.classList.add('dlm-swiped');
            else if (diff < -50) current.classList.remove('dlm-swiped');
            current = null;
        });
    }
    _setupSwipe();

    _startTimers();
}

// ─── Helpers ───



function _dlmEsc(str) {
    if (!str) return '';
    const d = document.createElement('div');
    d.textContent = str;
    return d.innerHTML;
}

function _dlmShortUrl(url) {
    if (!url) return '';
    if (url.startsWith('magnet:')) {
        const m = url.match(/dn=([^&]+)/);
        return m ? decodeURIComponent(m[1]).substring(0, 50) : 'Magnet link';
    }
    if (url.startsWith('torrent://')) {
        return url.substring(10);
    }
    try {
        const u = new URL(url);
        const path = u.pathname.split('/').pop() || u.hostname;
        return u.hostname + '/' + (path.length > 40 ? path.substring(0, 37) + '...' : path);
    } catch {
        return url.length > 60 ? url.substring(0, 57) + '...' : url;
    }
}



function _dlmFormatSpeed(bps) {
    if (!bps || bps <= 0) return '';
    return _dlmFormatBytes(bps) + '/s';
}

function _dlmFormatEta(seconds) {
    if (!seconds || seconds <= 0) return '';
    if (seconds < 60) return `${seconds}s`;
    if (seconds < 3600) {
        const m = Math.floor(seconds / 60);
        const s = seconds % 60;
        return `${m}m ${s}s`;
    }
    const h = Math.floor(seconds / 3600);
    const m = Math.floor((seconds % 3600) / 60);
    return `${h}h ${m}m`;
}

function _dlmStatusIcon(status, isTorrent) {
    switch (status) {
        case 'pending': return isTorrent
            ? '<i class="fas fa-magnet dl-si-pending"></i>'
            : '<i class="fas fa-clock dl-si-pending"></i>';
        case 'resolving': return '<i class="fas fa-gem fa-pulse dl-si-resolving"></i>';
        case 'torrent_uploading': return '<i class="fas fa-cloud-upload-alt fa-pulse dl-si-resolving"></i>';
        case 'torrent_downloading': return '<i class="fas fa-magnet fa-pulse dl-si-torrent"></i>';
        case 'downloading': return '<i class="fas fa-arrow-down fa-pulse dl-si-downloading"></i>';
        case 'paused': return '<i class="fas fa-pause-circle dl-si-pending"></i>';
        case 'completed': return '<i class="fas fa-check-circle dl-si-completed"></i>';
        case 'failed': return '<i class="fas fa-times-circle dl-si-failed"></i>';
        case 'cancelled': return '<i class="fas fa-ban dl-si-cancelled"></i>';
        default: return '<i class="fas fa-question-circle"></i>';
    }
}

function _dlmStatusLabel(status, dl) {
    switch (status) {
        case 'pending': return dl?.is_torrent ? t('Torrent — oczekuje') : t('Oczekuje');
        case 'resolving': return t('Rozwiązywanie linku...');
        case 'torrent_uploading': return t('Wysyłanie do debrid...');
        case 'torrent_downloading':
            let label = t('Debrid pobiera torrent');
            if (dl?.torrent_status) label += ` (${dl.torrent_status})`;
            return label;
        case 'downloading': return dl?.is_torrent
            ? `${t('Pobieranie plików')}${dl.torrent_files_total ? ` (${(dl.torrent_files_done||0)+1}/${dl.torrent_files_total})` : ''}`
            : 'Pobieranie';
        case 'paused': return t('Wstrzymano');
        case 'completed': return t('Zakończono');
        case 'failed': return t('Błąd');
        case 'cancelled': return t('Anulowano');
        default: return status;
    }
}

function _dlmFileTypeClass(filename, isTorrent) {
    if (isTorrent) return 'dlm-ftype-torrent';
    if (!filename) return '';
    const ext = filename.split('.').pop().toLowerCase();
    const VIDEO = ['mp4','mkv','avi','mov','wmv','flv','webm','m4v','mpg','mpeg','ts','vob'];
    const AUDIO = ['mp3','flac','wav','aac','ogg','wma','m4a','opus','alac'];
    const ARCHIVE = ['zip','rar','7z','tar','gz','bz2','xz','iso','cab','dmg'];
    const IMAGE = ['jpg','jpeg','png','gif','bmp','svg','webp','tiff','ico','heic'];
    const DOC = ['pdf','doc','docx','xls','xlsx','ppt','pptx','txt','rtf','odt','epub','mobi'];
    const CODE = ['js','py','html','css','json','xml','yml','yaml','sh','bat','sql','php','java','c','cpp','go','rs'];
    if (VIDEO.includes(ext)) return 'dlm-ftype-video';
    if (AUDIO.includes(ext)) return 'dlm-ftype-audio';
    if (ARCHIVE.includes(ext)) return 'dlm-ftype-archive';
    if (IMAGE.includes(ext)) return 'dlm-ftype-image';
    if (DOC.includes(ext)) return 'dlm-ftype-document';
    if (CODE.includes(ext)) return 'dlm-ftype-code';
    return '';
}
