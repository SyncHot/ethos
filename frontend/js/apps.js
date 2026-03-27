/* ═══════════════════════════════════════════════════════════
   EthOS — Built-in Applications
   File Manager, Dashboard, Docker Manager
   ═══════════════════════════════════════════════════════════ */

window.AppRegistry = window.AppRegistry || {};
const AppRegistry = window.AppRegistry;

// ═══════════════════════════════════════════════════════════
//  FILE MANAGER
// ═══════════════════════════════════════════════════════════

AppRegistry['file-manager'] = function (appDef, launchOpts) {
    const defaultHomePath = NAS.user?.home_path || `/home/${NAS.user?.username || 'home'}`;
    const sudoMode = !!(NAS.sudoMode || NAS.user?.sudo_mode);
    const requestedPath = launchOpts?.path;
    const initialSelect = launchOpts?.select;
    const defaultPath = defaultHomePath;
    const initialPath = (!requestedPath || requestedPath === '/') ? defaultPath : requestedPath;

    // If already open, navigate to the requested path and focus
    if (WM.windows.has('file-manager')) {
        const w = WM.windows.get('file-manager');
        focusWindow('file-manager');
        if (w.minimized) restoreWindow('file-manager');
        if (launchOpts?.path) {
            const fmBody = w.el.querySelector('.window-body');
            const targetPath = (launchOpts.path === '/' && !sudoMode) ? defaultHomePath : launchOpts.path;
            if (fmBody?._fmNavigateTo) fmBody._fmNavigateTo(targetPath);
        }
        return;
    }

    const state = {
        me: NAS.user?.username || 'home',
        homePath: defaultHomePath,
        sudoMode,
        path: initialPath,
        initialSelect,
        history: [initialPath],
        historyIndex: 0,
        items: [],
        selected: new Set(),
        sortCol: 'name',
        sortAsc: true,
        dragActive: false,
        clipboard: null,       // { mode: 'copy'|'cut', paths: [...], basePath: '...' }
        lastClickedIndex: -1,  // for shift-click range selection
        focusedIndex: -1,      // keyboard-focused item index
        selectMode: false,     // mobile select mode
        sambaShares: [],       // [{ name, path, ... }] loaded from backend
        favorites: [],         // [{ path, label }]
        photoFavorites: [],    // ['/path/to/img.jpg', ...]
        gallerySources: [],    // [{ path, label }] gallery folders
        // Pagination
        page: 0,
        pageSize: 200,
        // Search
        searchQuery: '',
        searchResults: null,   // null = not searching, [] = no results
        // Folder sizes
        dirSizes: {},          // { '/path': size }
        // Background dir-size job tracking
        _dirSizeJobs: {},      // { '/path': 'job_id' } for pending jobs
        _dirSizePollTimer: null,    // setTimeout handle for polling
        _dirSizePollInterval: 1500, // current poll interval (ms), grows with backoff
        // Lazy thumbnail IntersectionObserver
        _thumbObserver: null,
        // Navigation version counter — only the latest navigateTo renders
        _navVersion: 0,
    };

    createWindow('file-manager', {
        title: t('Menedżer plików'),
        icon: appDef.icon,
        iconColor: appDef.color,
        width: 1050,
        height: 650,
        onRender: (body) => renderFM(body, state),
    });
};

// ── FM listing prefetch cache (hover-prefetch optimisation) ──
const _fmPrefetchCache = new Map(); // path → { data, ts }
const _FM_PREFETCH_TTL = 25000;     // 25 s — slightly shorter than server cache TTL

function _fmPregenerateThumbs(path) {
    if (!path || path.startsWith('/__')) return;
    api('/files/pregenerate-thumbs', { method: 'POST', body: { path, w: 120, h: 120 } })
        .catch(() => {});  // fire-and-forget background pre-generation
}

// ── FM thumbnail concurrency limiter ──
// Avoids opening hundreds of simultaneous HTTP connections for thumbnails.
const _FM_THUMB_CONCURRENCY = 4;
let _fmThumbActive = 0;
const _fmThumbQueue = [];

function _fmThumbLoadNext() {
    while (_fmThumbActive < _FM_THUMB_CONCURRENCY && _fmThumbQueue.length) {
        const { img, src } = _fmThumbQueue.shift();
        if (!img.isConnected || img.src) continue;  // skip detached / already loaded imgs
        _fmThumbActive++;
        const onDone = () => { _fmThumbActive--; _fmThumbLoadNext(); };
        img.onload = onDone;
        img.onerror = onDone;
        img.src = src;
        img.removeAttribute('data-src');
        img.classList.remove('fm-thumb-lazy');
    }
}

function _fmThumbEnqueue(img, src) {
    _fmThumbQueue.push({ img, src });
    _fmThumbLoadNext();
}

function renderFM(body, state) {
    // View mode state
    if (!state.viewMode) {
        state.viewMode = localStorage.getItem('fmViewMode') || 'list';
    }
    function setViewMode(mode) {
        state.viewMode = mode;
        localStorage.setItem('fmViewMode', mode);
        renderFileList();
        updateViewModeButtons();
        if (mode === 'thumb') _fmPregenerateThumbs(state.path);
    }

    function openDLMForFolder(targetPath) {
        const destination = (targetPath || state.path || '/home').replace(/\/$/, '') || '/home';
        if (destination.startsWith('/__')) {
            toast(t('Nie można pobrać do tej lokalizacji'), 'warning');
            return;
        }
        openApp('download-manager', { dest_dir: destination });
    }
    body.innerHTML = `
        <div class="fm">
            <div class="fm-toolbar">
                <button class="fm-toolbar-btn fm-sidebar-toggle" id="fm-sidebar-toggle" title="${t('Nawigacja')}" aria-label="${t('Pokaż/ukryj panel nawigacji')}" aria-expanded="false"><i class="fas fa-bars"></i></button>
                <button class="fm-toolbar-btn" id="fm-back" title="${t('Wstecz')}" aria-label="${t('Wstecz')}"><i class="fas fa-arrow-left"></i></button>
                <button class="fm-toolbar-btn" id="fm-forward" title="${t('Dalej')}" aria-label="${t('Dalej')}"><i class="fas fa-arrow-right"></i></button>
                <button class="fm-toolbar-btn" id="fm-up" title="${t('Folder nadrzędny')}" aria-label="${t('Folder nadrzędny')}"><i class="fas fa-arrow-up"></i></button>
                <button class="fm-toolbar-btn" id="fm-refresh" title="${t('Odśwież')}" aria-label="${t('Odśwież')}"><i class="fas fa-sync-alt"></i></button>
                <div class="fm-toolbar-sep"></div>
                <div class="fm-breadcrumb" id="fm-breadcrumb" aria-label="${t('Ścieżka nawigacji')}" role="navigation"></div>
                <div class="fm-toolbar-sep"></div>
                <button class="fm-toolbar-btn" id="fm-newfolder" title="${t('New Folder')} (Ctrl+N)" aria-label="${t('New Folder')}"><i class="fas fa-folder-plus"></i></button>
                <button class="fm-toolbar-btn" id="fm-upload" title="${t('Prześlij pliki')} (Ctrl+U)" aria-label="${t('Prześlij pliki')}"><i class="fas fa-upload"></i></button>
                <button class="fm-toolbar-btn" id="fm-upload-folder" title="${t('Prześlij folder')}" aria-label="${t('Prześlij folder')}"><i class="fas fa-folder"></i><i class="fas fa-arrow-up fm-folder-upload-arrow"></i></button>
                <button class="fm-toolbar-btn" id="fm-download" title="${t('Pobierz zaznaczone')}" aria-label="${t('Pobierz zaznaczone')}"><i class="fas fa-download"></i></button>
                <button class="fm-toolbar-btn" id="fm-download-here" title="${t('Pobierz tutaj')}" aria-label="${t('Pobierz do bieżącego folderu')}"><i class="fas fa-folder-open"></i></button>
                <button class="fm-toolbar-btn" id="fm-delete" title="${t('Move to Trash')} (Delete)" aria-label="${t('Move to Trash')}"><i class="fas fa-trash"></i></button>
                <button class="fm-toolbar-btn fm-select-mode-btn" id="fm-select-mode-btn" title="${t('Tryb zaznaczania')}" aria-label="${t('Tryb zaznaczania')}" aria-pressed="false"><i class="fas fa-check-square"></i></button>
                <div class="fm-toolbar-sep"></div>
                <div class="fm-view-switcher" id="fm-view-switcher" role="group" aria-label="${t('Tryb widoku')}">
                    <button class="fm-view-btn" data-view="list" title="${t('Widok listy')}" aria-label="${t('Widok listy')}"><i class="fas fa-list"></i></button>
                    <button class="fm-view-btn" data-view="grid" title="${t('Widok ikon')}" aria-label="${t('Widok ikon')}"><i class="fas fa-th"></i></button>
                    <button class="fm-view-btn" data-view="thumb" title="${t('Miniatury')}" aria-label="${t('Widok miniatur')}"><i class="fas fa-th-large"></i></button>
                </div>
                <div class="fm-sort-dropdown" id="fm-sort-dropdown">
                    <button class="fm-toolbar-btn" id="fm-sort-btn" title="${t('Sortuj')}" aria-expanded="false" aria-haspopup="listbox" aria-controls="fm-sort-menu">
                        <i class="fas fa-sort-amount-down-alt"></i>
                        <span id="fm-sort-label">${t('Nazwa')}</span>
                        <i class="fas fa-chevron-down app-chevron-tiny"></i>
                    </button>
                    <div class="fm-sort-menu hidden" id="fm-sort-menu">
                        <div class="fm-sort-option" data-sort="name"><i class="fas fa-font"></i> ${t('Nazwa')}</div>
                        <div class="fm-sort-option" data-sort="size"><i class="fas fa-weight-hanging"></i> ${t('Rozmiar')}</div>
                        <div class="fm-sort-option" data-sort="modified"><i class="fas fa-clock"></i> ${t('Data modyfikacji')}</div>
                        <div class="fm-sort-option" data-sort="permissions"><i class="fas fa-lock"></i> ${t('Prawa')}</div>
                        <div class="fm-sort-divider"></div>
                        <div class="fm-sort-option" data-dir="asc"><i class="fas fa-sort-amount-up-alt"></i> ${t('Rosnąco')}</div>
                        <div class="fm-sort-option" data-dir="desc"><i class="fas fa-sort-amount-down-alt"></i> ${t('Malejąco')}</div>
                    </div>
                </div>
                <div class="fm-toolbar-sep"></div>
                <button class="fm-toolbar-btn" id="fm-analyze" title="${t('Analiza dysku')}" aria-label="${t('Analiza dysku')}"><i class="fas fa-chart-pie"></i></button>
                <button class="fm-toolbar-btn" id="fm-logs" title="${t('Logi zdarzeń')}" aria-label="${t('Logi zdarzeń')}"><i class="fas fa-history"></i></button>
                <button class="fm-toolbar-btn fm-shortcuts-btn" id="fm-shortcuts-btn" title="${t('Skróty klawiszowe')} (F1)" aria-label="${t('Skróty klawiszowe')}"><i class="fas fa-keyboard"></i></button>
                <div class="fm-toolbar-sep"></div>
                <div class="fm-search-box" id="fm-search-box" role="search">
                    <i class="fas fa-search fm-search-icon" aria-hidden="true"></i>
                    <input type="text" id="fm-search-input" placeholder="${t('Szukaj...')}" autocomplete="off" aria-label="${t('Szukaj plików')}">
                    <button class="fm-search-clear hidden" id="fm-search-clear" title="${t('Wyczyść')}" aria-label="${t('Wyczyść wyszukiwanie')}"><i class="fas fa-times"></i></button>
                </div>
            </div>
            <!-- Clipboard indicator -->
            <div class="fm-clipboard-bar hidden" id="fm-clipboard-bar">
                <i class="fas fa-clipboard"></i>
                <span id="fm-clipboard-text"></span>
                <button class="fm-toolbar-btn fm-btn-accent" id="fm-clipboard-paste"><i class="fas fa-paste"></i> ${t('Paste Here')}</button>
                <button class="fm-toolbar-btn" id="fm-clipboard-cancel"><i class="fas fa-times"></i></button>
            </div>
            <div class="fm-content">
                <div class="fm-sidebar" id="fm-sidebar"></div>
                <div class="fm-main fm-main-flex">
                    <div class="fm-list-header" id="fm-list-header">
                        <span class="fm-col-checkbox">
                            <label class="fm-checkbox-label" id="fm-header-select-all" title="${t('Zaznacz wszystko')}">
                                <input type="checkbox" id="fm-header-cb" aria-label="${t('Zaznacz wszystkie pliki')}">
                                <span class="fm-cb-custom"></span>
                            </label>
                        </span>
                        <span class="fm-header-columns" id="fm-header-columns">
                            <span data-sort="name" aria-sort="ascending" role="columnheader" tabindex="0">${t('Nazwa')} <i class="fas fa-sort"></i></span>
                            <span data-sort="size" aria-sort="none" role="columnheader" tabindex="0">${t('Rozmiar')} <i class="fas fa-sort"></i></span>
                            <span data-sort="modified" aria-sort="none" role="columnheader" tabindex="0">${t('Data modyfikacji')} <i class="fas fa-sort"></i></span>
                            <span data-sort="permissions" role="columnheader">${t('Prawa')}</span>
                        </span>
                        <div class="fm-header-selection hidden" id="fm-header-selection">
                            <span class="fm-sel-count" id="fm-sel-count">0 ${t('selected')}</span>
                            <button class="fm-sel-clear" id="fm-sel-clear" title="${t('Deselect')}"><i class="fas fa-times"></i> ${t('Deselect')}</button>
                            <div class="fm-sel-actions">
                                <button class="fm-toolbar-btn" id="fm-sel-copy" title="${t('Copy')}"><i class="fas fa-copy"></i> ${t('Copy')}</button>
                                <button class="fm-toolbar-btn" id="fm-sel-cut" title="${t('Cut')}"><i class="fas fa-cut"></i> ${t('Cut')}</button>
                                <button class="fm-toolbar-btn" id="fm-sel-download" title="${t('Pobierz')}"><i class="fas fa-download"></i></button>
                                <button class="fm-toolbar-btn fm-btn-danger" id="fm-sel-delete" title="${t('Move to Trash')}"><i class="fas fa-trash"></i> <span>${t('Move to Trash')}</span></button>
                            </div>
                        </div>
                    </div>
                    <div class="fm-file-list" id="fm-file-list"></div>
                    <div class="fm-pagination hidden" id="fm-pagination">
                        <button class="fm-page-btn" id="fm-page-prev" title="${t('Poprzednia strona')}"><i class="fas fa-chevron-left"></i></button>
                        <span class="fm-page-info" id="fm-page-info"></span>
                        <button class="fm-page-btn" id="fm-page-next" title="${t('Następna strona')}"><i class="fas fa-chevron-right"></i></button>
                    </div>
                    <div class="fm-statusbar" id="fm-statusbar" aria-live="polite" aria-atomic="true"></div>
                    <!-- Disk analytics panel (hidden) -->
                    <div class="fm-ana-panel fm-ana-overlay hidden" id="fm-ana-panel">
                        <div class="fm-ana-header">
                            <button class="fm-toolbar-btn" id="fm-ana-back-btn" title="${t('Zamknij analizę')}"><i class="fas fa-arrow-left"></i></button>
                            <span class="app-title-sm"><i class="fas fa-chart-pie app-btn-icon app-icon-accent"></i>${t('Analiza dysku')}</span>
                            <div class="app-flex-1"></div>
                            <button class="fm-toolbar-btn btn-green fm-toolbar-btn-compact" id="fm-ana-scan"><i class="fas fa-search"></i> ${t('Skanuj')}</button>
                        </div>
                        <div id="fm-ana-breadcrumbs" class="fm-ana-breadcrumbs"></div>
                        <div id="fm-ana-summary" class="fm-ana-summary hidden"></div>
                        <div id="fm-ana-tabs" class="fm-ana-tabs hidden">
                            <button class="fm-ana-sub-tab active" data-anatab="dirs"><i class="fas fa-folder"></i> ${t('Katalogi')}</button>
                            <button class="fm-ana-sub-tab" data-anatab="files"><i class="fas fa-file"></i> ${t('Największe pliki')}</button>
                        </div>
                        <div id="fm-ana-content" class="fm-ana-content">
                            <div class="app-empty">
                                <i class="fas fa-chart-pie app-empty-icon"></i>
                                ${t('Click')} <b>${t('Scan')}</b> ${t('to analyze current directory')}
                            </div>
                        </div>
                    </div>
                </div>
            </div>
        </div>
    `;

    // ─── Favorites ───
    async function loadFavorites() {
        try {
            const data = await api('/files/favorites');
            state.favorites = Array.isArray(data) ? data : [];
        } catch(e) {
            state.favorites = [];
        }
    }

    async function addFavorite(path, label) {
        try {
            const r = await api('/files/favorites', { method: 'POST', body: { path, label } });
            if (r.favorites) state.favorites = r.favorites;
            else await loadFavorites();
            renderSidebar();
            toast('Dodano do ulubionych', 'success');
        } catch(e) {
            toast(t('Błąd dodawania do ulubionych'), 'error');
        }
    }

    async function removeFavorite(path) {
        try {
            const r = await api('/files/favorites', { method: 'DELETE', body: { path } });
            if (r.favorites) state.favorites = r.favorites;
            else await loadFavorites();
            renderSidebar();
            toast(t('Usunięto z ulubionych'), 'success');
        } catch(e) {
            toast(t('Błąd usuwania z ulubionych'), 'error');
        }
    }

    function isFavorite(path) {
        return state.favorites.some(f => f.path === path);
    }

    // ─── Photo Favorites ───
    async function loadPhotoFavorites() {
        try {
            const data = await api('/photos/favorites');
            state.photoFavorites = Array.isArray(data) ? data : [];
        } catch (e) {
            state.photoFavorites = [];
        }
    }

    function isPhotoFavorite(filePath) {
        return state.photoFavorites.includes(filePath);
    }

    function isSpecialPath(path) {
        return path === '/__photo_favorites__' || path === '/__trash__' || path === '/__shared_with_me__';
    }

    function isRegularPath(path = state.path) {
        return !isSpecialPath(path);
    }

    function isAtHomeRoot(path = state.path) {
        if (state.sudoMode) return path === '/';
        return path === state.homePath;
    }

    function joinCurrentPath(name) {
        const base = (state.path || '/').replace(/\/$/, '');
        return `${base}/${name}`;
    }

    async function togglePhotoFavorite(filePath) {
        const isFav = isPhotoFavorite(filePath);
        try {
            const r = await api('/photos/favorites', {
                method: isFav ? 'DELETE' : 'POST',
                body: { path: filePath }
            });
            if (r.favorites) state.photoFavorites = r.favorites;
            else await loadPhotoFavorites();
            toast(isFav ? t('Usunięto z ulubionych zdjęć') : t('Dodano do ulubionych zdjęć'), 'success');
        } catch {
            toast(t('Błąd operacji na ulubionych'), 'error');
        }
    }

    // ─── Sidebar ───
    function renderSidebar() {
        const sidebar = body.querySelector('#fm-sidebar');
        state.sudoMode = !!(NAS.sudoMode || NAS.user?.sudo_mode);
        const isAdmin = NAS.user?.role === 'admin';
        const roots = [
            { icon: 'fa-home', label: state.me, path: state.homePath },
        ];
        // Home-only sandbox: do not expose global media/mnt roots in File Manager.

        const favHtml = state.favorites.length > 0 ? `
            <div class="fm-sidebar-section">
                <div class="fm-sidebar-label"><i class="fas fa-star"></i> ${t('Ulubione')}</div>
                ${state.favorites.map(f => `
                    <div class="fm-fav-row${state.path === f.path ? ' active' : ''}">
                        <button class="fm-tree-item fm-fav-item${state.path === f.path ? ' active' : ''}" data-path="${f.path}" title="${f.path}">
                            <i class="fas fa-folder app-icon-accent"></i> ${f.label}
                        </button>
                        <button class="fm-fav-remove" data-fav-path="${f.path}" title="${t('Usuń z ulubionych')}">
                            <i class="fas fa-times"></i>
                        </button>
                    </div>
                `).join('')}
            </div>
            <div class="fm-sidebar-divider"></div>
        ` : '';

        sidebar.innerHTML = `
            ${favHtml}
            <div class="fm-sidebar-section">
                <button class="fm-tree-item fm-photo-favs-btn${state.path === '/__photo_favorites__' ? ' active' : ''}" data-path="/__photo_favorites__">
                    <i class="fas fa-heart app-icon-heart"></i> ${t('Favorite Photos')}
                </button>
            </div>
            <div class="fm-sidebar-divider"></div>
            <div class="fm-sidebar-section">
                <button class="fm-tree-item fm-shared-with-me-btn${state.path === '/__shared_with_me__' ? ' active' : ''}" data-path="/__shared_with_me__">
                    <i class="fas fa-share-alt app-icon-share"></i> ${t('Shared with Me')}
                </button>
                <button class="fm-tree-item" onclick="openApp('naslink')">
                    <i class="fas fa-network-wired app-icon-violet"></i> Transfer NAS
                </button>
            </div>
            <div class="fm-sidebar-divider"></div>
            ${state.sudoMode ? `<button class="fm-tree-item${state.path === '/' ? ' active' : ''}" data-path="/">
                <i class="fas fa-hard-drive"></i> System (/)
            </button>` : ''}
            ${roots.map(r => `
                <button class="fm-tree-item${state.path.startsWith(r.path) ? ' active' : ''}" data-path="${r.path}">
                    <i class="fas ${r.icon}"></i> ${r.label}
                </button>
            `).join('')}
            <div class="fm-sidebar-divider"></div>
            <button class="fm-tree-item fm-trash-btn${state.path === '/__trash__' ? ' active' : ''}" data-path="/__trash__">
                <i class="fas fa-trash-alt app-icon-danger"></i> ${t('Kosz')}
            </button>
            ${isAdmin ? `<div class="fm-sidebar-divider"></div>
            <button class="fm-tree-item fm-dup-btn" id="fm-open-dup-app">
                <i class="fas fa-clone app-icon-purple"></i> ${t('Duplikaty zdjęć')}
            </button>` : ''}
        `;
        sidebar.querySelectorAll('.fm-tree-item[data-path]').forEach(btn => {
            btn.addEventListener('click', () => {
                console.log(`[FM] sidebar click: "${btn.textContent.trim()}" → path="${btn.dataset.path}"`);
                navigateTo(btn.dataset.path);
            });
        });
        sidebar.querySelector('#fm-open-dup-app')?.addEventListener('click', () => {
            const dupApp = NAS.apps?.find(a => a.id === 'duplicates') || { id: 'duplicates', name: t('Duplikaty zdjęć'), icon: 'fa-clone', color: '#a78bfa', type: 'builtin' };
            openApp(dupApp);
        });
        sidebar.querySelectorAll('.fm-fav-remove').forEach(btn => {
            btn.addEventListener('click', (e) => {
                e.stopPropagation();
                removeFavorite(btn.dataset.favPath);
            });
        });
    }

    // ─── Breadcrumb ───
    // Helper: get full path for item (supports photo favorites with embedded path)
    function itemFullPath(nameOrItem) {
        const name = typeof nameOrItem === 'string' ? nameOrItem : nameOrItem.name;
        if (state.path === '/__photo_favorites__') {
            const item = typeof nameOrItem === 'object' ? nameOrItem : state.items.find(i => i.name === name);
            if (item && item.path) return item.path;
        }
        return joinCurrentPath(name);
    }

    function renderBreadcrumb() {
        const bc = body.querySelector('#fm-breadcrumb');
        if (state.path === '/__photo_favorites__') {
            bc.innerHTML = `<button class="fm-breadcrumb-item" data-path="/__photo_favorites__"><i class="fas fa-heart app-icon-heart"></i> ${t('Favorite Photos')}</button>`;
            bc.querySelector('.fm-breadcrumb-item')?.addEventListener('click', () => navigateTo('/__photo_favorites__'));
            return;
        }
        if (state.path === '/__trash__') {
            bc.innerHTML = `<button class="fm-breadcrumb-item" data-path="/__trash__"><i class="fas fa-trash-alt app-icon-danger"></i> ${t('Trash')}</button>`;
            bc.querySelector('.fm-breadcrumb-item')?.addEventListener('click', () => navigateTo('/__trash__'));
            return;
        }
        if (state.path === '/__shared_with_me__') {
            bc.innerHTML = `<button class="fm-breadcrumb-item" data-path="/__shared_with_me__"><i class="fas fa-share-alt app-icon-share"></i> ${t('Shared with Me')}</button>`;
            bc.querySelector('.fm-breadcrumb-item')?.addEventListener('click', () => navigateTo('/__shared_with_me__'));
            return;
        }

        if (!state.sudoMode && state.path.startsWith(state.homePath)) {
            const rel = state.path.slice(state.homePath.length).replace(/^\//, '');
            const relParts = rel ? rel.split('/') : [];
            const crumbs = [`<button class="fm-breadcrumb-item" data-path="${state.homePath}"><i class="fas fa-home"></i> ${state.me}</button>`];
            relParts.forEach((part, i) => {
                const path = `${state.homePath}/${relParts.slice(0, i + 1).join('/')}`;
                crumbs.push(`<span class="fm-breadcrumb-sep"><i class="fas fa-chevron-right"></i></span>`);
                crumbs.push(`<button class="fm-breadcrumb-item" data-path="${path}">${part}</button>`);
            });
            bc.innerHTML = crumbs.join('');
        } else {
            const parts = state.path === '/' ? [''] : state.path.split('/');
            bc.innerHTML = parts.map((part, i) => {
                const path = i === 0 ? '/' : parts.slice(0, i + 1).join('/');
                const label = i === 0 ? '<i class="fas fa-server"></i>' : part;
                return `<button class="fm-breadcrumb-item" data-path="${path}">${label}</button>${i < parts.length - 1 ? '<span class="fm-breadcrumb-sep"><i class="fas fa-chevron-right"></i></span>' : ''}`;
            }).join('');
        }
        bc.querySelectorAll('.fm-breadcrumb-item').forEach(btn => {
            btn.addEventListener('click', () => navigateTo(btn.dataset.path));
        });
    }

    // ─── File List ───
    function getFileIcon(item) {
        if (item.is_dir) {
            if (item.locked) return 'fa-lock';
            if (item.protected) return 'fa-folder-open';
            return 'fa-folder';
        }
        const ext = item.name.split('.').pop().toLowerCase();
        const map = {
            'jpg,jpeg,png,gif,bmp,webp,svg,ico': 'fa-file-image',
            'mp4,mkv,avi,mov,wmv,flv,webm': 'fa-file-video',
            'mp3,wav,flac,aac,ogg,wma,m4a': 'fa-file-audio',
            'pdf': 'fa-file-pdf',
            'zip,rar,7z,tar,gz,bz2,xz': 'fa-file-archive',
            'js,ts,py,html,css,json,xml,sh,yaml,yml,md,sql,php,java,c,cpp,h,rb,go,rs': 'fa-file-code',
            'txt,log,csv,ini,cfg,conf': 'fa-file-alt',
            'doc,docx,odt': 'fa-file-word',
            'xls,xlsx,ods': 'fa-file-excel',
            'ppt,pptx,odp': 'fa-file-powerpoint',
        };
        for (const [exts, icon] of Object.entries(map)) {
            if (exts.split(',').includes(ext)) return icon;
        }
        return 'fa-file';
    }

    function sortItems(items) {
        return [...items].sort((a, b) => {
            // Folders first
            if (a.is_dir !== b.is_dir) return a.is_dir ? -1 : 1;
            let cmp = 0;
            switch (state.sortCol) {
                case 'name': cmp = a.name.localeCompare(b.name, 'pl'); break;
                case 'size': cmp = a.size - b.size; break;
                case 'modified': cmp = a.modified - b.modified; break;
                case 'permissions': cmp = (a.permissions || '').localeCompare(b.permissions || ''); break;
            }
            return state.sortAsc ? cmp : -cmp;
        });
    }

    function getShareForItem(item) {
        if (!item.is_dir) return null;
        const fullPath = itemFullPath(item);
        return state.sambaShares.find(s => s.path === fullPath || s.path === fullPath.toLowerCase());
    }

    async function loadSambaShares() {
        try {
            const data = await api('/storage/samba/shares');
            state.sambaShares = Array.isArray(data) ? data : (data.shares || []);
        } catch (e) {
            state.sambaShares = [];
        }
    }

    async function _fmLoadGallerySources() {
        try {
            const data = await api('/gallery/folders');
            state.gallerySources = Array.isArray(data) ? data : [];
        } catch (e) {
            state.gallerySources = [];
        }
    }

    function renderFileList() {
        console.log(`[FM] renderFileList: path="${state.path}", items=${state.items?.length}`);
        const list = body.querySelector('#fm-file-list');
        list.style.opacity = '1';  // restore from loading indicator
        // Set ARIA attributes on list container
        list.setAttribute('role', 'listbox');
        list.setAttribute('tabindex', '0');
        list.setAttribute('aria-multiselectable', 'true');
        list.setAttribute('aria-label', t('Pliki i foldery'));
        // Ensure file list is visible (may have been hidden by trash/shared views)
        list.style.display = '';
        // Restore list header visibility after special views (trash/shared hide it)
        const listHeader = body.querySelector('#fm-list-header');
        if (listHeader) listHeader.style.display = state.viewMode === 'list' ? '' : 'none';
        // Restore select mode class after re-render
        list.classList.toggle('fm-select-mode', !!state.selectMode);
        const allItems = state.searchResults !== null ? state.searchResults : state.items;
        const sorted = sortItems(allItems);

        // ── Pagination ──
        const totalPages = Math.max(1, Math.ceil(sorted.length / state.pageSize));
        if (state.page >= totalPages) state.page = totalPages - 1;
        if (state.page < 0) state.page = 0;
        const pageStart = state.page * state.pageSize;
        const pageItems = sorted.slice(pageStart, pageStart + state.pageSize);

        // Pagination UI
        const pagEl = body.querySelector('#fm-pagination');
        if (sorted.length > state.pageSize) {
            pagEl.classList.remove('hidden');
            body.querySelector('#fm-page-info').textContent = `${state.page + 1} / ${totalPages}  (${sorted.length} elem.)`;
            body.querySelector('#fm-page-prev').disabled = state.page <= 0;
            body.querySelector('#fm-page-next').disabled = state.page >= totalPages - 1;
        } else {
            pagEl.classList.add('hidden');
        }

        if (!sorted.length) {
            list.innerHTML = state.searchResults !== null
                ? `<div class="fm-empty"><i class="fas fa-search"></i><span>${t('Brak wyników')}</span></div>`
                : '<div class="fm-empty"><i class="fas fa-folder-open"></i><span>Folder jest pusty</span></div>';
            return;
        }

        // Helper: folder size display
        const _dirSize = (item) => {
            if (!item.is_dir) return formatBytes(item.size);
            const p = itemFullPath(item);
            if (p in state.dirSizes) return formatBytes(state.dirSizes[p]);
            return '—';
        };

        // Helper: for search results, show the parent path
        const _searchPath = (item) => {
            if (state.searchResults === null || !item.path) return '';
            const parent = item.path.substring(0, item.path.lastIndexOf('/')) || '/';
            return ` <span class="fm-search-path" title="${item.path}">${parent}/</span>`;
        };

        if (state.viewMode === 'list') {
            list.className = 'fm-file-list fm-list-view';
            list.innerHTML = pageItems.map((item, localIdx) => {
                const idx = pageStart + localIdx;
                const icon = getFileIcon(item);
                const selected = state.selected.has(item.name);
                const focused = idx === state.focusedIndex;
                const share = getShareForItem(item);
                const sharedBadge = share ? ` <span class="fm-shared-badge" title="${t('Udostępniony jako:')} ${share.name}"><i class="fas fa-share-alt"></i></span>` : '';
                const galBadge = item.is_dir && state.gallerySources.some(s => s.path === itemFullPath(item)) ? ` <span class="fm-shared-badge app-icon-gallery" title="${t('Folder multimedialny')}"><i class="fas fa-images"></i></span>` : '';
                const lockBadge = item.protected ? ` <span class="fm-shared-badge" title="${item.locked ? t('Folder chroniony hasłem (zablokowany)') : t('Folder chroniony hasłem (odblokowany)')}" style="color:${item.locked ? 'var(--danger)' : 'var(--success, #22c55e)'}"><i class="fas ${item.locked ? 'fa-lock' : 'fa-lock-open'}"></i></span>` : '';
                return `
                    <div class="fm-file-item${selected ? ' selected' : ''}${focused ? ' fm-focused' : ''}" id="fm-item-${idx}" role="option" aria-selected="${selected}" data-name="${item.name}" data-isdir="${item.is_dir}" data-idx="${idx}"${item.path ? ` data-path="${item.path}"` : ''}>
                        <div class="fm-col-checkbox">
                            <label class="fm-checkbox-label" data-cb-name="${item.name}" aria-label="${t('Zaznacz')} ${item.name}">
                                <input type="checkbox" ${selected ? 'checked' : ''}>
                                <span class="fm-cb-custom"></span>
                            </label>
                        </div>
                        <div class="fm-file-name">
                            <i class="fas ${icon}${item.locked ? ' app-text-danger' : ''}" aria-hidden="true"></i>
                            <span>${item.name}</span>${_searchPath(item)}${lockBadge}${sharedBadge}${galBadge}
                        </div>
                        <div class="fm-file-size">${_dirSize(item)}</div>
                        <div class="fm-file-date">${formatDate(item.modified)}</div>
                        <div class="fm-file-perms" title="${item.permissions_symbolic || item.permissions || ''}${item.owner ? ` | ${item.owner}:${item.group}` : ''}">
                            <span style="font-family:monospace">${item.permissions_symbolic || ''}</span>
                            <span style="opacity:0.6;font-size:0.85em;margin-left:6px">${item.permissions_octal || item.permissions || ''}</span>
                        </div>
                    </div>
                `;
            }).join('');
        } else if (state.viewMode === 'grid') {
            list.className = 'fm-file-list fm-grid-view';
            list.innerHTML = pageItems.map((item, localIdx) => {
                const idx = pageStart + localIdx;
                const icon = getFileIcon(item);
                const selected = state.selected.has(item.name);
                const focused = idx === state.focusedIndex;
                const share = getShareForItem(item);
                const sharedBadge = share ? ` <span class="fm-shared-badge" title="${t('Udostępniony jako:')} ${share.name}"><i class="fas fa-share-alt"></i></span>` : '';
                const galBadge = item.is_dir && state.gallerySources.some(s => s.path === itemFullPath(item)) ? ` <span class="fm-shared-badge app-icon-gallery" title="${t('Folder multimedialny')}"><i class="fas fa-images"></i></span>` : '';
                const lockBadge = item.protected ? ` <span class="fm-shared-badge" title="${item.locked ? t('Zablokowany') : t('Odblokowany')}" style="color:${item.locked ? 'var(--danger)' : 'var(--success, #22c55e)'}"><i class="fas ${item.locked ? 'fa-lock' : 'fa-lock-open'}"></i></span>` : '';
                return `
                    <div class="fm-grid-item${selected ? ' selected' : ''}${focused ? ' fm-focused' : ''}" id="fm-item-${idx}" role="option" aria-selected="${selected}" data-name="${item.name}" data-isdir="${item.is_dir}" data-idx="${idx}"${item.path ? ` data-path="${item.path}"` : ''}>
                        <div class="fm-grid-icon"><i class="fas ${icon}${item.locked ? ' app-text-danger' : ''}" aria-hidden="true"></i></div>
                        <div class="fm-grid-label">${item.name}${lockBadge}${sharedBadge}${galBadge}</div>
                        <div class="fm-grid-checkbox">
                            <label class="fm-checkbox-label" data-cb-name="${item.name}" aria-label="${t('Zaznacz')} ${item.name}">
                                <input type="checkbox" ${selected ? 'checked' : ''}>
                                <span class="fm-cb-custom"></span>
                            </label>
                        </div>
                    </div>
                `;
            }).join('');
        } else if (state.viewMode === 'thumb') {
            list.className = 'fm-file-list fm-thumb-view';
            list.innerHTML = pageItems.map((item, localIdx) => {
                const idx = pageStart + localIdx;
                const isImage = /\.(jpg|jpeg|png|gif|bmp|webp|svg|ico)$/i.test(item.name);
                const selected = state.selected.has(item.name);
                const focused = idx === state.focusedIndex;
                const share = getShareForItem(item);
                const sharedBadge = share ? ` <span class="fm-shared-badge" title="${t('Udostępniony jako:')} ${share.name}"><i class="fas fa-share-alt"></i></span>` : '';
                const galBadge = item.is_dir && state.gallerySources.some(s => s.path === itemFullPath(item)) ? ` <span class="fm-shared-badge app-icon-gallery" title="${t('Folder multimedialny')}"><i class="fas fa-images"></i></span>` : '';
                const lockBadge = item.protected ? ` <span class="fm-shared-badge" title="${item.locked ? t('Zablokowany') : t('Odblokowany')}" style="color:${item.locked ? 'var(--danger)' : 'var(--success, #22c55e)'}"><i class="fas ${item.locked ? 'fa-lock' : 'fa-lock-open'}"></i></span>` : '';
                let thumbHtml = '';
                if (isImage && !item.is_dir) {
                    // Lazy-loaded image: src will be set by IntersectionObserver
                    const imgSrc = `/api/files/preview?path=${encodeURIComponent(itemFullPath(item))}&w=120&h=120`;
                    thumbHtml = `<img data-src="${imgSrc}" class="fm-thumb-img fm-thumb-lazy" alt="${item.name}">`;
                } else {
                    const icon = getFileIcon(item);
                    thumbHtml = `<div class="fm-thumb-icon"><i class="fas ${icon}${item.locked ? ' app-text-danger' : ''}" aria-hidden="true"></i></div>`;
                }
                return `
                    <div class="fm-thumb-item${selected ? ' selected' : ''}${focused ? ' fm-focused' : ''}" id="fm-item-${idx}" role="option" aria-selected="${selected}" data-name="${item.name}" data-isdir="${item.is_dir}" data-idx="${idx}"${item.path ? ` data-path="${item.path}"` : ''}>
                        <div class="fm-thumb-preview">${thumbHtml}</div>
                        <div class="fm-thumb-label">${item.name}${lockBadge}${sharedBadge}${galBadge}</div>
                        <div class="fm-thumb-checkbox">
                            <label class="fm-checkbox-label" data-cb-name="${item.name}" aria-label="${t('Zaznacz')} ${item.name}">
                                <input type="checkbox" ${selected ? 'checked' : ''}>
                                <span class="fm-cb-custom"></span>
                            </label>
                        </div>
                    </div>
                `;
            }).join('');
        }

        // Update status bar
        const _src = state.searchResults !== null ? state.searchResults : state.items;
        const dirs = _src.filter(i => i.is_dir).length;
        const files = _src.filter(i => !i.is_dir).length;
        const totalSize = _src.filter(i => !i.is_dir).reduce((s, i) => s + i.size, 0);
        const selectedCount = state.selected.size;

        let selectedSizeInfo = '';
        if (selectedCount) {
            const selSize = _src.filter(i => state.selected.has(i.name) && !i.is_dir).reduce((s, i) => s + i.size, 0);
            selectedSizeInfo = ` | ${t('Zaznaczono:')} ${selectedCount} (${formatBytes(selSize)})`;
        }
        const searchLabel = state.searchResults !== null ? `${t('Wyniki')} (${_src.length}) — ` : '';
        body.querySelector('#fm-statusbar').textContent =
            searchLabel + `${_src.length} ${t('elementów')} (${dirs} ${t('folderów')}, ${files} ${t('plików')}) — ${formatBytes(totalSize)}` + selectedSizeInfo;

        // Update selection bar visibility
        updateSelectionBar();

        // Update header checkbox state
        const headerCb = body.querySelector('#fm-header-cb');
        if (headerCb) {
            headerCb.checked = state.items.length > 0 && state.selected.size === state.items.length;
            headerCb.indeterminate = state.selected.size > 0 && state.selected.size < state.items.length;
        }

        // Show/hide list header depending on view mode (re-check after render)
        {
            const lh = body.querySelector('#fm-list-header');
            if (lh) lh.style.display = state.viewMode === 'list' ? '' : 'none';
        }
        // Show sort dropdown only for grid/thumb, hide for list
        const sortDropdown = body.querySelector('#fm-sort-dropdown');
        if (sortDropdown) {
            sortDropdown.style.display = state.viewMode === 'list' ? 'none' : '';
        }
        updateSortLabel();

        // ── Lazy thumbnail loading via IntersectionObserver ──
        if (state.viewMode === 'thumb') {
            // Disconnect previous observer if any
            if (state._thumbObserver) {
                state._thumbObserver.disconnect();
                state._thumbObserver = null;
            }
            const lazyImgs = list.querySelectorAll('img.fm-thumb-lazy');
            if (lazyImgs.length > 0) {
                const obs = new IntersectionObserver((entries) => {
                    entries.forEach(entry => {
                        if (entry.isIntersecting) {
                            const img = entry.target;
                            if (img.dataset.src) {
                                // Use concurrency-limited loader to avoid hundreds of parallel requests
                                _fmThumbEnqueue(img, img.dataset.src);
                            }
                            obs.unobserve(img);
                        }
                    });
                }, { root: list, rootMargin: '200px', threshold: 0 });
                lazyImgs.forEach(img => obs.observe(img));
                state._thumbObserver = obs;
            }
        } else if (state._thumbObserver) {
            state._thumbObserver.disconnect();
            state._thumbObserver = null;
        }

        // Update aria-activedescendant for keyboard focus
        const activeId = state.focusedIndex >= 0 ? `fm-item-${state.focusedIndex}` : '';
        list.setAttribute('aria-activedescendant', activeId);

    }

    /* ─── Lightweight selection update (no DOM rebuild) ─── */
    function updateSelection() {
        const list = body.querySelector('#fm-file-list');
        const itemSelector = '.fm-file-item, .fm-grid-item, .fm-thumb-item';
        list.querySelectorAll(itemSelector).forEach(el => {
            const name = el.dataset.name;
            const isSelected = state.selected.has(name);
            el.classList.toggle('selected', isSelected);
            el.setAttribute('aria-selected', isSelected ? 'true' : 'false');
            const cb = el.querySelector('input[type="checkbox"]');
            if (cb) cb.checked = isSelected;
        });

        // Status bar
        const dirs = state.items.filter(i => i.is_dir).length;
        const files = state.items.filter(i => !i.is_dir).length;
        const totalSize = state.items.filter(i => !i.is_dir).reduce((s, i) => s + i.size, 0);
        let selectedSizeInfo = '';
        if (state.selected.size) {
            const selSize = state.items.filter(i => state.selected.has(i.name) && !i.is_dir).reduce((s, i) => s + i.size, 0);
            selectedSizeInfo = ` | ${t('Zaznaczono:')} ${state.selected.size} (${formatBytes(selSize)})`;
        }
        body.querySelector('#fm-statusbar').textContent =
            `${state.items.length} ${t('elementów')} (${dirs} ${t('folderów')}, ${files} ${t('plików')}) — ${formatBytes(totalSize)}` + selectedSizeInfo;

        updateSelectionBar();

        const headerCb = body.querySelector('#fm-header-cb');
        if (headerCb) {
            headerCb.checked = state.items.length > 0 && state.selected.size === state.items.length;
            headerCb.indeterminate = state.selected.size > 0 && state.selected.size < state.items.length;
        }
    }

    function updateSelectionBar() {
        const selPanel = body.querySelector('#fm-header-selection');
        const colPanel = body.querySelector('#fm-header-columns');
        const header = body.querySelector('#fm-list-header');
        const count = state.selected.size;
        // In select mode, use floating batch bar — hide header selection panel
        if (state.selectMode) {
            selPanel.classList.add('hidden');
            colPanel.classList.remove('hidden');
            header.classList.remove('fm-header-selecting');
        } else if (count > 0) {
            selPanel.classList.remove('hidden');
            colPanel.classList.add('hidden');
            header.classList.add('fm-header-selecting');
            body.querySelector('#fm-sel-count').textContent = count + ' ' + t('selected');
        } else {
            selPanel.classList.add('hidden');
            colPanel.classList.remove('hidden');
            header.classList.remove('fm-header-selecting');
        }
        updateFMBatchBar();
        updateClipboardBar();
    }

    function updateFMBatchBar() {
        const main = body.querySelector('.fm-main');
        if (!main) return;
        let bar = main.querySelector('.fm-batch-bar');
        const count = state.selected.size;
        // Show batch bar only in select mode with items selected
        if (!state.selectMode || count === 0) {
            if (bar) bar.remove();
            return;
        }
        if (!bar) {
            bar = document.createElement('div');
            bar.className = 'fm-batch-bar';
            bar.innerHTML = `
                <span class="fm-batch-count"></span>
                <button class="fm-toolbar-btn fm-batch-copy" title="${t('Copy')}"><i class="fas fa-copy"></i> ${t('Copy')}</button>
                <button class="fm-toolbar-btn fm-batch-cut" title="${t('Cut')}"><i class="fas fa-cut"></i> ${t('Cut')}</button>
                <button class="fm-toolbar-btn fm-batch-download" title="${t('Pobierz')}"><i class="fas fa-download"></i></button>
                <button class="fm-toolbar-btn fm-btn-danger fm-batch-delete" title="${t('Move to Trash')}"><i class="fas fa-trash"></i></button>
                <button class="fm-batch-cancel">${t('Anuluj')}</button>
            `;
            bar.querySelector('.fm-batch-copy').addEventListener('click', clipboardCopy);
            bar.querySelector('.fm-batch-cut').addEventListener('click', clipboardCut);
            bar.querySelector('.fm-batch-download').addEventListener('click', downloadSelected);
            bar.querySelector('.fm-batch-delete').addEventListener('click', deleteSelected);
            bar.querySelector('.fm-batch-cancel').addEventListener('click', () => {
                setFMSelectMode(false);
            });
            main.appendChild(bar);
        }
        bar.querySelector('.fm-batch-count').textContent = count + ' ' + t('zaznaczonych');
    }

    /* ─── Keyboard focus indicator (roving highlight) ─── */
    function setFocusedIndex(idx, scrollIntoView = true) {
        state.focusedIndex = idx;
        const list = body.querySelector('#fm-file-list');
        const itemSel = '.fm-file-item, .fm-grid-item, .fm-thumb-item';
        const activeId = idx >= 0 ? `fm-item-${idx}` : '';
        list.setAttribute('aria-activedescendant', activeId);
        list.querySelectorAll(itemSel).forEach(el => {
            const isActive = parseInt(el.dataset.idx) === idx;
            el.classList.toggle('fm-focused', isActive);
            if (isActive && scrollIntoView) {
                el.scrollIntoView({ block: 'nearest', behavior: 'smooth' });
            }
        });
    }

    function updateClipboardBar() {
        const bar = body.querySelector('#fm-clipboard-bar');
        if (state.clipboard) {
            bar.classList.remove('hidden');
            const modeText = state.clipboard.mode === 'copy' ? t('Copied') : t('Cut');
            body.querySelector('#fm-clipboard-text').textContent = modeText + ': ' + state.clipboard.paths.length + ' ' + t('item(s)');
        } else {
            bar.classList.add('hidden');
        }
    }

    function previewFile(name) {
        const ext = name.split('.').pop().toLowerCase();
        const imageExts = ['jpg', 'jpeg', 'png', 'gif', 'bmp', 'webp', 'svg', 'ico'];
        const textExts = ['txt', 'log', 'md', 'json', 'xml', 'yaml', 'yml', 'csv', 'ini', 'cfg', 'conf', 'sh', 'py', 'js', 'ts', 'html', 'css', 'sql', 'php', 'java', 'c', 'cpp', 'h', 'rb', 'go', 'rs'];
        const docExts = ['docx'];
        const videoExts = ['mp4', 'webm', 'mkv', 'avi', 'mov'];
        const audioExts = ['mp3', 'wav', 'ogg', 'flac', 'aac', 'm4a'];
        const mediaExts = [...imageExts, ...videoExts];

        // If it's a document file, open in the editor
        if (docExts.includes(ext)) {
            const filePath = itemFullPath(name);
            const appDef = NAS.apps?.find(a => a.id === 'doc-editor') || { id: 'doc-editor', icon: 'fa-file-word', color: '#2563eb' };
            openApp(appDef, { path: filePath, filename: name });
            return;
        }

        // If it's a media file, open the media viewer
        if (mediaExts.includes(ext)) {
            openMediaViewer(name);
            return;
        }

        const path = itemFullPath(name);

        if (audioExts.includes(ext)) {
            createWindow('preview-' + name, {
                title: name,
                icon: 'fa-music',
                iconColor: '#34d399',
                width: 400,
                height: 150,
                minHeight: 150,
                singleton: false,
                content: `<div class="app-player-wrap"><audio controls autoplay class="app-w-full"><source src="/api/files/preview?path=${encodeURIComponent(path)}"></audio></div>`,
            });
        } else if (textExts.includes(ext)) {
            // Open in code editor
            const filePath = itemFullPath(name);
            const appDef = NAS.apps?.find(a => a.id === 'code-editor') || { id: 'code-editor', icon: 'fa-code', color: '#22d3ee' };
            openApp(appDef, { path: filePath, filename: name });
        } else {
            // Just download
            downloadSelected();
        }
    }

    /* ─── Media Viewer (images + videos with navigation & delete) ─── */
    function openMediaViewer(startName) {
        const imageExts = ['jpg', 'jpeg', 'png', 'gif', 'bmp', 'webp', 'svg', 'ico'];
        const videoExts = ['mp4', 'webm', 'mkv', 'avi', 'mov'];
        const mediaExts = [...imageExts, ...videoExts];

        // Collect all media files in current folder, sorted same as file list
        let mediaFiles = sortItems(state.items).filter(item => {
            if (item.is_dir) return false;
            const e = item.name.split('.').pop().toLowerCase();
            return mediaExts.includes(e);
        });

        if (!mediaFiles.length) return;

        let currentIdx = mediaFiles.findIndex(f => f.name === startName);
        if (currentIdx < 0) currentIdx = 0;

        const winId = 'media-viewer';

        function getFilePath(name) {
            // Photo favorites have full path on each item
            const file = mediaFiles[currentIdx];
            if (file && file.path) return file.path;
            return joinCurrentPath(name);
        }

        function isImage(name) {
            return imageExts.includes(name.split('.').pop().toLowerCase());
        }

        function renderMedia(bodyEl) {
            const file = mediaFiles[currentIdx];
            if (!file) return;
            const filePath = getFilePath(file.name);
            const src = `/api/files/preview?path=${encodeURIComponent(filePath)}`;

            // Update window title
            const winData = WM.windows.get(winId);
            if (winData) {
                const titleSpan = winData.el.querySelector('.window-title span');
                if (titleSpan) titleSpan.textContent = file.name;
            }

            let mediaHtml;
            if (isImage(file.name)) {
                mediaHtml = `<img class="mv-media mv-img" src="${src}" alt="${file.name}" draggable="false">`;
            } else {
                mediaHtml = `<video class="mv-media mv-video" controls autoplay><source src="${src}"></video>`;
            }

            bodyEl.innerHTML = `
                <div class="mv-container">
                    <div class="mv-content">${mediaHtml}</div>
                    <div class="mv-overlay mv-nav-left" title="${t('Poprzedni')}"><i class="fas fa-chevron-left"></i></div>
                    <div class="mv-overlay mv-nav-right" title="${t('Następny')}"><i class="fas fa-chevron-right"></i></div>
                    <div class="mv-topbar">
                        <span class="mv-counter">${currentIdx + 1} / ${mediaFiles.length}</span>
                        <span class="mv-filename">${file.name}</span>
                        <div class="mv-actions">
                            <button class="mv-btn mv-fav-btn${isPhotoFavorite(filePath) ? ' active' : ''}" id="mv-favorite" title="${t('Ulubione')} (F)"><i class="fas fa-heart"></i></button>
                            <button class="mv-btn" id="mv-delete" title="${t('Usuń')} (Delete)"><i class="fas fa-trash"></i></button>
                            <button class="mv-btn" id="mv-download" title="${t('Pobierz')}"><i class="fas fa-download"></i></button>
                            <button class="mv-btn" id="mv-close" title="${t('Zamknij')} (Esc)"><i class="fas fa-times"></i></button>
                        </div>
                    </div>
                    <div class="mv-bottombar">
                        <button class="mv-nav-btn" id="mv-prev" ${currentIdx <= 0 ? 'disabled' : ''}><i class="fas fa-arrow-left"></i> ${t('Poprzedni')}</button>
                        <span class="mv-info">${file.is_dir ? '' : formatBytes(file.size)}</span>
                        <button class="mv-nav-btn" id="mv-next" ${currentIdx >= mediaFiles.length - 1 ? 'disabled' : ''}>${t('Następny')} <i class="fas fa-arrow-right"></i></button>
                    </div>
                </div>
            `;

            // Nav click handlers
            bodyEl.querySelector('.mv-nav-left')?.addEventListener('click', goPrev);
            bodyEl.querySelector('.mv-nav-right')?.addEventListener('click', goNext);
            bodyEl.querySelector('#mv-prev')?.addEventListener('click', goPrev);
            bodyEl.querySelector('#mv-next')?.addEventListener('click', goNext);
            bodyEl.querySelector('#mv-favorite')?.addEventListener('click', toggleFavCurrent);
            bodyEl.querySelector('#mv-delete')?.addEventListener('click', deleteCurrent);
            bodyEl.querySelector('#mv-download')?.addEventListener('click', downloadCurrent);
            bodyEl.querySelector('#mv-close')?.addEventListener('click', () => closeWindow(winId));

            // Touch swipe support (mobile) — skip when touching video controls
            const container = bodyEl.querySelector('.mv-container');
            let touchStartX = 0, touchStartY = 0, touchDeltaX = 0, touchDeltaY = 0;
            let swipeDir = null; // 'h' horizontal, 'v' vertical, null = undecided
            let touchOnVideo = false; // true if touch started on <video>
            const SWIPE_THRESHOLD = 50;
            const LOCK_ANGLE = 8; // px movement before locking direction

            container.addEventListener('touchstart', (e) => {
                if (e.touches.length !== 1) return;
                // Don't intercept touches on video element (let native controls work)
                touchOnVideo = e.target.closest('video') !== null;
                if (touchOnVideo) return;
                touchStartX = e.touches[0].clientX;
                touchStartY = e.touches[0].clientY;
                touchDeltaX = 0;
                touchDeltaY = 0;
                swipeDir = null;
            }, { passive: true });

            container.addEventListener('touchmove', (e) => {
                if (touchOnVideo) return; // let video handle its own gestures
                if (e.touches.length !== 1) return;
                touchDeltaX = e.touches[0].clientX - touchStartX;
                touchDeltaY = e.touches[0].clientY - touchStartY;
                const ax = Math.abs(touchDeltaX), ay = Math.abs(touchDeltaY);

                // Lock direction after small movement
                if (!swipeDir && (ax > LOCK_ANGLE || ay > LOCK_ANGLE)) {
                    swipeDir = ax >= ay ? 'h' : 'v';
                }
                if (!swipeDir) return;
                e.preventDefault();

                const media = container.querySelector('.mv-content');
                if (!media) return;
                if (swipeDir === 'h') {
                    media.style.transform = `translateX(${touchDeltaX}px)`;
                } else {
                    // Vertical: translate Y + opacity hint
                    const progress = Math.min(Math.abs(touchDeltaY) / 150, 1);
                    media.style.transform = `translateY(${touchDeltaY}px)`;
                    media.style.opacity = `${1 - progress * 0.4}`;
                }
            }, { passive: false });

            container.addEventListener('touchend', () => {
                if (touchOnVideo) { touchOnVideo = false; return; }
                const media = container.querySelector('.mv-content');
                if (!swipeDir || !media) {
                    if (media) { media.style.transform = ''; media.style.opacity = ''; }
                    return;
                }

                if (swipeDir === 'h') {
                    if (Math.abs(touchDeltaX) >= SWIPE_THRESHOLD) {
                        media.style.transition = 'transform 0.2s ease-out';
                        media.style.transform = `translateX(${touchDeltaX > 0 ? '100%' : '-100%'})`;
                        setTimeout(() => {
                            if (touchDeltaX > 0) goPrev();
                            else goNext();
                        }, 150);
                    } else {
                        // Snap back
                        media.style.transition = 'transform 0.2s ease-out';
                        media.style.transform = '';
                        setTimeout(() => { media.style.transition = ''; }, 200);
                    }
                } else {
                    // Vertical swipe
                    if (Math.abs(touchDeltaY) >= SWIPE_THRESHOLD) {
                        // Animate out
                        media.style.transition = 'transform 0.2s ease-out, opacity 0.2s ease-out';
                        media.style.transform = `translateY(${touchDeltaY > 0 ? '100%' : '-100%'})`;
                        media.style.opacity = '0';
                        setTimeout(() => {
                            if (touchDeltaY > 0) {
                                // Swipe down → delete without confirmation
                                deleteCurrentNoConfirm();
                            } else {
                                // Swipe up → toggle favorite
                                toggleFavCurrent();
                            }
                        }, 150);
                    } else {
                        // Snap back
                        media.style.transition = 'transform 0.2s ease-out, opacity 0.2s ease-out';
                        media.style.transform = '';
                        media.style.opacity = '';
                        setTimeout(() => { media.style.transition = ''; }, 200);
                    }
                }
                swipeDir = null;
            }, { passive: true });
        }

        function goPrev() {
            if (currentIdx > 0) {
                currentIdx--;
                const bodyEl = document.getElementById('win-body-' + winId);
                if (bodyEl) renderMedia(bodyEl);
            }
        }

        function goNext() {
            if (currentIdx < mediaFiles.length - 1) {
                currentIdx++;
                const bodyEl = document.getElementById('win-body-' + winId);
                if (bodyEl) renderMedia(bodyEl);
            }
        }

        async function deleteCurrent() {
            const file = mediaFiles[currentIdx];
            if (!file) return;
            const sure = await confirmDialog(t('Usuń'), `${t('Czy na pewno usunąć')} "${file.name}"?`);
            if (!sure) return;
            await _doDelete(file);
        }

        async function deleteCurrentNoConfirm() {
            const file = mediaFiles[currentIdx];
            if (!file) return;
            await _doDelete(file);
        }

        async function _doDelete(file) {
            const filePath = getFilePath(file.name);
            try {
                await api('/files/delete', { method: 'DELETE', body: { paths: [filePath] } });
                toast(`Przeniesiono do kosza: "${file.name}"`, 'success');

                // Also remove from photo favorites if it was favorited
                if (isPhotoFavorite(filePath)) {
                    try { await api('/photos/favorites', { method: 'DELETE', body: { path: filePath } }); } catch {}
                    await loadPhotoFavorites();
                }

                // Remove from media list
                mediaFiles.splice(currentIdx, 1);
                // Also refresh file manager state
                state.items = state.items.filter(i => i.name !== file.name);
                state.selected.delete(file.name);
                renderFileList();

                if (mediaFiles.length === 0) {
                    closeWindow(winId);
                    return;
                }
                if (currentIdx >= mediaFiles.length) currentIdx = mediaFiles.length - 1;
                const bodyEl = document.getElementById('win-body-' + winId);
                if (bodyEl) renderMedia(bodyEl);
            } catch {
                toast(t('Błąd usuwania'), 'error');
            }
        }

        function downloadCurrent() {
            const file = mediaFiles[currentIdx];
            if (!file) return;
            const filePath = getFilePath(file.name);
            const a = document.createElement('a');
            a.href = `/api/files/download?path=${encodeURIComponent(filePath)}`;
            a.download = file.name;
            document.body.appendChild(a);
            a.click();
            a.remove();
        }

        async function toggleFavCurrent() {
            const file = mediaFiles[currentIdx];
            if (!file) return;
            const filePath = getFilePath(file.name);
            await togglePhotoFavorite(filePath);
            // Update the heart button state
            const btn = document.getElementById('mv-favorite');
            if (btn) btn.classList.toggle('active', isPhotoFavorite(filePath));
        }

        // Keyboard handler
        function onKeyDown(e) {
            // Only handle if this window is focused
            const winData = WM.windows.get(winId);
            if (!winData || WM.activeId !== winId) return;

            if (e.key === 'ArrowLeft') { e.preventDefault(); goPrev(); }
            else if (e.key === 'ArrowRight') { e.preventDefault(); goNext(); }
            else if (e.key === 'Delete') { e.preventDefault(); deleteCurrent(); }
            else if (e.key === 'Escape') { e.preventDefault(); closeWindow(winId); }
            else if (e.key === 'f' || e.key === 'F') { e.preventDefault(); toggleFavCurrent(); }
        }

        // Close existing viewer if open (remove old keydown listener first)
        if (WM.windows.has(winId)) closeWindow(winId);

        document.addEventListener('keydown', onKeyDown);

        createWindow(winId, {
            title: mediaFiles[currentIdx].name,
            icon: 'fa-images',
            iconColor: '#a78bfa',
            width: 900,
            height: 650,
            singleton: false,
            onRender: (bodyEl) => renderMedia(bodyEl),
            onClose: () => {
                document.removeEventListener('keydown', onKeyDown);
            },
        });

        // Auto-maximize on small screens (phones)
        if (window.innerWidth <= 768) {
            toggleMaximize(winId);
        }
    }

    function escapeHtml(str) {
        return str.replace(/&/g, '&amp;').replace(/</g, '&lt;').replace(/>/g, '&gt;');
    }

    // ─── Context menu ───
    function showFMContextMenu(x, y) {
        let existing = document.querySelector('.fm-ctx-menu');
        if (existing) existing.remove();

        const menu = document.createElement('div');
        menu.className = 'fm-ctx-menu';
        menu.style.cssText = `position:fixed;left:${x}px;top:${y}px;z-index:9999;background:var(--bg-elevated);border:1px solid var(--border);border-radius:var(--r-md);box-shadow:var(--shadow-lg);padding:4px 0;min-width:200px;max-height:80vh;overflow-y:auto;`;

        const selCount = state.selected.size;
        const singleSelected = selCount === 1 ? [...state.selected][0] : null;
        const singleItem = singleSelected ? state.items.find(i => i.name === singleSelected) : null;

        const items = [];

        // New file / New folder — on empty space right-click (no selection)
        if (selCount === 0) {
            items.push({ icon: 'fa-folder-plus', label: t('Nowy folder'), action: 'newfolder', cls: 'accent' });
            items.push({ icon: 'fa-file-medical', label: t('Nowy plik'), action: 'newfile', cls: 'accent' });
            items.push({ sep: true });
            items.push({ icon: 'fa-upload', label: t('Prześlij pliki'), action: 'upload' });
            items.push({ icon: 'fa-folder', label: t('Prześlij folder'), action: 'upload-folder' });
            items.push({ sep: true });
        }

        // Open — only for single selection
        if (singleSelected) {
            items.push({ icon: 'fa-folder-open', label: t('Otwórz'), action: 'open' });
        }

        // Download
        if (selCount > 0) {
            const dlLabel = selCount > 1 ? t('Pobierz') + ` (${selCount}) jako ZIP` : (singleItem?.is_dir ? t('Pobierz folder jako ZIP') : t('Pobierz'));
            items.push({ icon: 'fa-download', label: dlLabel, action: 'download' });
            items.push({ icon: 'fa-network-wired', label: t('Prześlij do NAS'), action: 'send-to-nas' });
        }

        items.push({ sep: true });

        // Select all
        items.push({ icon: 'fa-check-double', label: t('Zaznacz wszystko'), action: 'selectall' });

        // Open in Gallery
        if (singleItem && singleItem.is_dir) {
             items.push({ icon: 'fa-images', label: t('Pokaż w Galerii'), action: 'open-gallery-folder' });
        }
        const imageExts = ['jpg', 'jpeg', 'png', 'gif', 'bmp', 'webp', 'svg', 'ico'];
        const ext = singleItem ? singleItem.name.split('.').pop().toLowerCase() : '';
        if (singleItem && !singleItem.is_dir && imageExts.includes(ext)) {
             items.push({ icon: 'fa-image', label: t('Pokaż w Galerii'), action: 'open-gallery-file' });
        }

        items.push({ sep: true });

        // Copy / Cut
        items.push({ icon: 'fa-copy', label: selCount > 1 ? t('Kopiuj') + ` (${selCount})` : t('Kopiuj'), action: 'copy' });
        items.push({ icon: 'fa-cut', label: selCount > 1 ? t('Wytnij') + ` (${selCount})` : t('Wytnij'), action: 'cut' });

        // Paste — if clipboard has items
        if (state.clipboard) {
            const pMode = state.clipboard.mode === 'copy' ? t('Wklej') : t('Przenieś tutaj');
            items.push({ icon: 'fa-paste', label: pMode + ' (' + state.clipboard.paths.length + ')', action: 'paste' });
        }

        items.push({ sep: true });

        // Archive operations
        if (selCount > 0) {
            items.push({ icon: 'fa-file-archive', label: selCount > 1 ? t('Kompresuj do ZIP') + ` (${selCount})` : t('Kompresuj do ZIP'), action: 'compress-zip' });
            items.push({ icon: 'fa-file-archive', label: selCount > 1 ? t('Kompresuj do TAR.GZ') + ` (${selCount})` : t('Kompresuj do TAR.GZ'), action: 'compress-targz' });
        }
        // Extract — single archive file
        if (singleItem && !singleItem.is_dir) {
            const ext = singleItem.name.split('.').pop().toLowerCase();
            const archiveExts = ['zip', 'rar', '7z', 'gz', 'tgz', 'bz2', 'xz', 'tar', 'cab', 'iso'];
            const fullName = singleItem.name.toLowerCase();
            if (archiveExts.includes(ext) || fullName.endsWith('.tar.gz') || fullName.endsWith('.tar.bz2') || fullName.endsWith('.tar.xz') || /\.part\d+\.rar$/i.test(fullName) || /\.r\d+$/i.test(fullName)) {
                items.push({ icon: 'fa-box-open', label: t('Rozpakuj tutaj'), action: 'extract' });
            }
        }

        items.push({ sep: true });

        // Favorites — only for single folder
        if (singleItem && singleItem.is_dir) {
            const folderPath = itemFullPath(singleItem);
            if (isFavorite(folderPath)) {
                items.push({ icon: 'fa-star', label: t('Usuń z ulubionych'), action: 'fav-remove', cls: 'accent' });
            } else {
                items.push({ icon: 'fa-star', label: t('Dodaj do ulubionych'), action: 'fav-add', cls: 'accent' });
            }
        }
        // Also allow adding current directory from empty-space right-click
        if (selCount === 0 && isRegularPath() && !isAtHomeRoot()) {
            if (isFavorite(state.path)) {
                items.push({ icon: 'fa-star', label: t('Usuń ten folder z ulubionych'), action: 'fav-remove-current', cls: 'accent' });
            } else {
                items.push({ icon: 'fa-star', label: t('Dodaj ten folder do ulubionych'), action: 'fav-add-current', cls: 'accent' });
            }
        }

        // Share / Unshare via Samba — only for single folder

        // Public share link — for single file or folder
        if (singleSelected) {
            items.push({ icon: 'fa-link', label: t('Udostępnij (Link)'), action: 'share-link', cls: 'accent' });
        }

        // Share / Unshare via Samba — only for single folder
        if (singleItem && singleItem.is_dir) {
            const existingShare = getShareForItem(singleItem);
            if (existingShare) {
                items.push({ icon: 'fa-share-alt-slash', label: t('Cofnij udostępnianie') + ` („${existingShare.name}”)`, action: 'samba-unshare', cls: 'danger' });
            }
            items.push({ icon: 'fa-share-alt', label: t('Udostępnij (Samba)'), action: 'samba-share', cls: 'accent' });
        }

        items.push({ sep: true });

        // Rename — only single item
        if (singleSelected) {
            items.push({ icon: 'fa-pen', label: t('Zmień nazwę'), action: 'rename' });
        }

        // Gallery folder toggle — only for single folder
        if (singleItem && singleItem.is_dir) {
            const galPath = itemFullPath(singleItem);
            const isGal = state.gallerySources && state.gallerySources.some(s => s.path === galPath);
            if (isGal) {
                items.push({ icon: 'fa-images', label: t('Otwórz w galerii'), action: 'gallery-open', cls: 'accent' });
                items.push({ icon: 'fa-images', label: t('Usuń z galerii'), action: 'gallery-remove', cls: 'accent' });
            } else {
                items.push({ icon: 'fa-images', label: t('Oznacz jako multimedialny'), action: 'gallery-add', cls: 'accent' });
            }
        }

        // Open image/video file in gallery
        if (singleItem && !singleItem.is_dir) {
            const _fmExt = singleItem.name.split('.').pop().toLowerCase();
            const _fmMediaExts = ['jpg', 'jpeg', 'png', 'gif', 'bmp', 'webp', 'svg', 'mp4', 'webm', 'mkv', 'avi', 'mov'];
            if (_fmMediaExts.includes(_fmExt)) {
                items.push({ icon: 'fa-images', label: t('Otwórz w galerii'), action: 'open-in-gallery', cls: 'accent' });
            }
        }

        // Scan for duplicates — only for single folder
        if (singleItem && singleItem.is_dir) {
            items.push({ icon: 'fa-clone', label: t('Skanuj duplikaty'), action: 'scan-duplicates', cls: 'accent' });
        }

        // Folder password protection — only for single folder
        if (singleItem && singleItem.is_dir) {
            items.push({ sep: true });
            if (singleItem.protected) {
                items.push({ icon: 'fa-lock-open', label: t('Usuń hasło folderu'), action: 'folder-pw-remove', cls: 'danger' });
                if (singleItem.locked) {
                    items.push({ icon: 'fa-unlock', label: t('Odblokuj folder'), action: 'folder-unlock', cls: 'accent' });
                } else {
                    items.push({ icon: 'fa-lock', label: t('Zablokuj ponownie'), action: 'folder-lock', cls: 'accent' });
                }
            } else {
                items.push({ icon: 'fa-lock', label: t('Zabezpiecz hasłem'), action: 'folder-pw-set', cls: 'accent' });
            }
        }

        // Properties — single item
        if (singleItem) {
            items.push({ sep: true });
            items.push({ icon: 'fa-info-circle', label: t('Właściwości'), action: 'properties' });
        }

        // Delete
        items.push({ sep: true });
        if (selCount > 0) {
            items.push({ icon: 'fa-cloud-upload-alt', label: selCount > 1 ? t('Transferuj do NAS') + ` (${selCount})` : t('Transferuj do NAS'), action: 'transfer-remote', cls: 'accent' });
        }
        items.push({ icon: 'fa-trash', label: selCount > 1 ? t('Do kosza') + ` (${selCount})` : t('Do kosza'), action: 'delete', cls: 'danger' });

        menu.innerHTML = items.map(item => {
            if (item.sep) return '<div class="app-divider"></div>';
            const color = item.cls === 'danger' ? 'var(--danger)' : item.cls === 'accent' ? 'var(--accent)' : item.cls === 'accent-purple' ? 'var(--accent-purple)' : 'var(--text-primary)';
            const iconColor = item.cls === 'danger' ? 'var(--danger)' : item.cls === 'accent' ? 'var(--accent)' : item.cls === 'accent-purple' ? 'var(--accent-purple)' : 'var(--text-muted)';
            return `<button data-action="${item.action}" class="app-ctx-btn" style="color:${color}" onmouseover="this.style.background='rgba(255,255,255,0.06)'" onmouseout="this.style.background='none'">
                <i class="fas ${item.icon} app-icon-fixed" style="color:${iconColor}"></i>
                ${item.label}
            </button>`;
        }).join('');

        menu.addEventListener('click', async (e) => {
            const btn = e.target.closest('[data-action]');
            if (!btn) return;
            menu.remove();
            switch (btn.dataset.action) {
                case 'open':
                    const selected = [...state.selected][0];
                    const item = state.items.find(i => i.name === selected);
                    if (item?.is_dir) navigateTo(itemFullPath(item));
                    else if (item) previewFile(item.name);
                    break;
                case 'download': downloadSelected(); break;
                case 'dl-download-here': {
                    if (singleItem && singleItem.is_dir) {
                        openDLMForFolder(itemFullPath(singleItem));
                    }
                    break;
                }
                case 'rename': renameSelected(); break;
                case 'delete': deleteSelected(); break;
                case 'send-to-nas': {
                    const paths = [...state.selected].map(name => {
                        const it = state.items.find(i => i.name === name);
                        return it ? itemFullPath(it) : null;
                    }).filter(Boolean);
                    openApp('naslink', { tab: 'transfer', paths });
                    break;
                }
                case 'open-gallery-folder':
                    if (singleItem) openApp('gallery', { folder: itemFullPath(singleItem) });
                    break;
                case 'open-gallery-file':
                    openApp('gallery', { folder: state.path, file: itemFullPath(singleItem) });
                    break;
                case 'copy': clipboardCopy(); break;
                case 'cut': clipboardCut(); break;
                case 'paste': clipboardPaste(); break;
                case 'selectall': selectAll(); break;
                case 'newfolder': createNewFolder(); break;
                case 'newfile': createNewFile(); break;
                case 'upload': uploadFiles(); break;
                case 'upload-folder': uploadFolder(); break;
                case 'fav-add': {
                    const s = [...state.selected][0];
                    const fp = joinCurrentPath(s);
                    addFavorite(fp, s);
                    break;
                }
                case 'fav-remove': {
                    const s2 = [...state.selected][0];
                    const fp2 = joinCurrentPath(s2);
                    removeFavorite(fp2);
                    break;
                }
                case 'fav-add-current': addFavorite(state.path, state.path.split('/').pop()); break;
                case 'fav-remove-current': removeFavorite(state.path); break;
                case 'samba-share': shareSamba(); break;
                case 'samba-unshare': unshareSamba(); break;
                case 'share-link': shareLink(); break;
                case 'compress-zip': compressSelected('zip'); break;
                case 'compress-targz': compressSelected('tar.gz'); break;
                case 'extract': extractSelected(); break;
                case 'gallery-add': {
                    const gs = [...state.selected][0];
                    const gPath = itemFullPath(gs);
                    const gLabel = typeof gs === 'string' ? gs : gs.name || gPath.split('/').pop();
                    try {
                        await api('/gallery/folders', { method: 'POST', body: { path: gPath, label: gLabel } });
                        toast('Folder dodany do galerii', 'success');
                        _fmLoadGallerySources();
                    } catch (e) { toast(t('Błąd: ') + (e.message || e), 'error'); }
                    break;
                }
                case 'gallery-open': {
                    const goItem = [...state.selected][0];
                    const goPath = itemFullPath(goItem);
                    const galApp = NAS.apps?.find(a => a.id === 'gallery') || { id: 'gallery', type: 'builtin' };
                    openApp(galApp, { folder: goPath });
                    break;
                }
                case 'gallery-remove': {
                    const gr = [...state.selected][0];
                    const grPath = itemFullPath(gr);
                    try {
                        await api('/gallery/folders', { method: 'DELETE', body: { path: grPath } });
                        toast(t('Folder usunięty z galerii'), 'info');
                        _fmLoadGallerySources();
                    } catch (e) { toast(t('Błąd: ') + (e.message || e), 'error'); }
                    break;
                }
                case 'scan-duplicates': {
                    const s = [...state.selected][0];
                    const folderPath = itemFullPath(s);
                    const dupApp = NAS.apps?.find(a => a.id === 'duplicates') || { id: 'duplicates', name: t('Duplikaty zdjęć'), icon: 'fa-clone', color: '#a78bfa', type: 'builtin' };
                    openApp(dupApp, { scanPath: folderPath, forceScan: true });
                    break;
                }
                case 'folder-pw-set': {
                    const fpPath = itemFullPath([...state.selected][0]);
                    const pw = await promptDialog(t('Zabezpiecz hasłem'), t('Podaj hasło dla folderu (min. 4 znaki):'));
                    if (!pw) break;
                    if (pw.length < 4) { toast(t('Hasło musi mieć minimum 4 znaki'), 'error'); break; }
                    const pw2 = await promptDialog(t('Potwierdź hasło'), t('Powtórz hasło:'));
                    if (pw !== pw2) { toast(t('Hasła nie są identyczne'), 'error'); break; }
                    const setRes = await api('/files/folder-password', { method: 'POST', body: { path: fpPath, password: pw } });
                    if (setRes.ok) { toast(t('Hasło ustawione'), 'success'); navigateTo(state.path); }
                    else toast(setRes.error || t('Błąd'), 'error');
                    break;
                }
                case 'folder-pw-remove': {
                    const frPath = itemFullPath([...state.selected][0]);
                    const isAdmin = NAS.user?.role === 'admin';
                    let rpw = '';
                    if (isAdmin) {
                        // Admin override check
                        const override = await new Promise(resolve => {
                            const overlay = document.createElement('div');
                            overlay.className = 'app-modal-overlay';
                            overlay.style.cssText = 'position:fixed;inset:0;background:rgba(0,0,0,0.6);z-index:9999;display:flex;align-items:center;justify-content:center';
                            overlay.innerHTML = `
                                <div class="app-modal" style="background:var(--bg-surface,#1e1e2e);padding:20px;border-radius:10px;border:1px solid var(--border,#333);min-width:300px">
                                    <h3 style="margin-top:0">${t('Usuń hasło folderu')}</h3>
                                    <p>${t('Podaj aktualne hasło:')}</p>
                                    <input type="password" id="pw-remove-input" class="app-input" style="width:100%;margin-bottom:10px" autofocus>
                                    <div style="margin-bottom:15px">
                                        <label style="display:flex;align-items:center;gap:8px;font-size:0.9em;color:var(--text-muted)">
                                            <input type="checkbox" id="pw-admin-force"> ${t('Wymuś usunięcie (Admin)')}
                                        </label>
                                    </div>
                                    <div style="display:flex;justify-content:flex-end;gap:10px">
                                        <button id="pw-cancel" class="app-btn">${t('Anuluj')}</button>
                                        <button id="pw-confirm" class="app-btn app-btn-primary">${t('Usuń')}</button>
                                    </div>
                                </div>
                            `;
                            document.body.appendChild(overlay);

                            const inp = overlay.querySelector('#pw-remove-input');
                            const forceCb = overlay.querySelector('#pw-admin-force');

                            const close = (res) => { overlay.remove(); resolve(res); };
                            overlay.querySelector('#pw-cancel').onclick = () => close(null);
                            overlay.querySelector('#pw-confirm').onclick = () => close({ pw: inp.value, force: forceCb.checked });
                            inp.onkeydown = e => { if(e.key === 'Enter') close({ pw: inp.value, force: forceCb.checked }); };
                        });

                        if (!override) break;
                        const rmRes = await api('/files/folder-password', { method: 'DELETE', body: { path: frPath, password: override.pw, force: override.force } });
                        if (rmRes.ok) { toast(t('Hasło usunięte'), 'success'); navigateTo(state.path); }
                        else toast(rmRes.error || t('Nieprawidłowe hasło'), 'error');
                        break;
                    }

                    rpw = await promptDialog(t('Usuń hasło folderu'), t('Podaj aktualne hasło:'));
                    if (!rpw) break;
                    const rmRes = await api('/files/folder-password', { method: 'DELETE', body: { path: frPath, password: rpw } });
                    if (rmRes.ok) { toast(t('Hasło usunięte'), 'success'); navigateTo(state.path); }
                    else toast(rmRes.error || t('Nieprawidłowe hasło'), 'error');
                    break;
                }
                case 'folder-unlock': {
                    const fuPath = itemFullPath([...state.selected][0]);
                    const upw = await promptDialog(t('Odblokuj folder'), t('Podaj hasło:'));
                    if (!upw) break;
                    const ulRes = await api('/files/folder-unlock', { method: 'POST', body: { path: fuPath, password: upw } });
                    if (ulRes.ok) { toast(t('Folder odblokowany'), 'success'); navigateTo(state.path); }
                    else toast(ulRes.error || t('Nieprawidłowe hasło'), 'error');
                    break;
                }
                case 'folder-lock': {
                    const flPath = itemFullPath([...state.selected][0]);
                    const lkRes = await api('/files/folder-lock', { method: 'POST', body: { path: flPath } });
                    if (lkRes.ok) { toast(t('Folder zablokowany'), 'success'); navigateTo(state.path); }
                    else toast(lkRes.error || t('Błąd'), 'error');
                    break;
                }
                case 'properties': {
                    const propItem = singleItem;
                    if (!propItem) break;
                    const propPath = itemFullPath(propItem);
                    await showPropertiesDialog(propItem, propPath);
                    break;
                }
                case 'transfer-remote': transferToRemoteNAS(); break;
            }
        });

        document.body.appendChild(menu);
        // Clamp to viewport
        requestAnimationFrame(() => {
            const rect = menu.getBoundingClientRect();
            if (rect.bottom > window.innerHeight) {
                menu.style.top = Math.max(4, window.innerHeight - rect.height - 4) + 'px';
            }
            if (rect.right > window.innerWidth) {
                menu.style.left = Math.max(4, window.innerWidth - rect.width - 4) + 'px';
            }
        });
        setTimeout(() => {
            document.addEventListener('click', function handler() {
                menu.remove();
                document.removeEventListener('click', handler);
            }, { once: true });
        }, 50);
    }

    // ─── Trash View (Kosz) ───
    async function renderTrashView() {
        const list = body.querySelector('#fm-file-list');
        const header = body.querySelector('#fm-list-header');
        if (header) header.style.display = 'none';

        list.className = 'fm-file-list fm-trash-view';
        list.innerHTML = `<div class="fm-empty"><i class="fas fa-spinner fa-spin"></i><span>${t('Ładowanie kosza…')}</span></div>`;

        let data;
        try {
            data = await api('/files/trash');
        } catch {
            list.innerHTML = `<div class="fm-empty"><i class="fas fa-exclamation-triangle"></i><span>${t('Błąd ładowania kosza')}</span></div>`;
            return;
        }

        const items = data.items || [];
        const retentionDays = data.retention_days || 30;

        if (!items.length) {
            list.innerHTML = `<div class="fm-empty"><i class="fas fa-trash-alt"></i><span>${t('Trash is empty')}</span></div>`;
            body.querySelector('#fm-statusbar').textContent = t('Trash is empty');
            return;
        }

        const totalSize = items.reduce((s, i) => s + (i.size || 0), 0);

        list.innerHTML = `
            <div class="fm-trash-header">
                <div class="fm-trash-info">
                    <i class="fas fa-info-circle"></i>
                    ${t('Elementy w koszu są automatycznie usuwane po')} ${retentionDays} ${t('dniach.')}
                </div>
                <div class="fm-trash-actions">
                    <button class="fm-trash-btn fm-trash-restore-all" title="${t('Przywróć wszystko')}">
                        <i class="fas fa-undo"></i> ${t('Przywróć wszystko')}
                    </button>
                    <button class="fm-trash-btn fm-trash-empty-btn" title="${t('Opróżnij kosz')}">
                        <i class="fas fa-trash"></i> ${t('Opróżnij kosz')}
                    </button>
                </div>
            </div>
            <div class="fm-trash-items">
                ${items.map(item => {
                    const icon = item.is_dir ? 'fa-folder' : 'fa-file';
                    const daysClass = item.days_left <= 3 ? 'fm-trash-days-danger' : item.days_left <= 7 ? 'fm-trash-days-warn' : '';
                    const isImage = /\.(jpg|jpeg|png|gif|bmp|webp|svg|ico)$/i.test(item.name);
                    const thumbSrc = isImage && !item.is_dir ? `/api/files/trash/preview?id=${encodeURIComponent(item.trash_id)}&w=48&h=48` : '';
                    return `
                        <div class="fm-trash-item" data-trash-id="${item.trash_id}">
                            <div class="fm-trash-item-icon">${isImage && !item.is_dir ? `<img src="${thumbSrc}" class="fm-trash-thumb" alt="">` : `<i class="fas ${icon}"></i>`}</div>
                            <div class="fm-trash-item-info">
                                <div class="fm-trash-item-name">${item.name}</div>
                                <div class="fm-trash-item-meta">
                                    <span class="fm-trash-item-path" title="${item.original_path}"><i class="fas fa-folder-open"></i> ${item.original_path}</span>
                                    <span><i class="fas fa-calendar"></i> ${item.deleted_date}</span>
                                    <span>${formatBytes(item.size || 0)}</span>
                                    <span class="fm-trash-days ${daysClass}"><i class="fas fa-clock"></i> ${item.days_left} ${t('dni')}</span>
                                </div>
                            </div>
                            <div class="fm-trash-item-actions">
                                <button class="fm-trash-btn fm-trash-restore-one" data-trash-id="${item.trash_id}" title="${t('Przywróć')}">
                                    <i class="fas fa-undo"></i>
                                </button>
                                <button class="fm-trash-btn fm-trash-delete-one" data-trash-id="${item.trash_id}" title="${t('Usuń trwale')}">
                                    <i class="fas fa-times"></i>
                                </button>
                            </div>
                        </div>
                    `;
                }).join('')}
            </div>
        `;

        body.querySelector('#fm-statusbar').textContent =
            `${t('Kosz:')} ${items.length} ${t('elementów')} — ${formatBytes(totalSize)}`;

        // Restore all
        list.querySelector('.fm-trash-restore-all')?.addEventListener('click', async () => {
            const sure = await confirmDialog(t('Przywróć wszystko'), `${t('Przywrócić')} ${items.length} ${t('elementów z kosza?')}`);
            if (!sure) return;
            try {
                const r = await api('/files/trash/restore', { method: 'POST', body: { trash_ids: items.map(i => i.trash_id) } });
                toast(`${t('Przywrócono')} ${(r.restored || []).length} ${t('elementów')}`, 'success');
                if (r.errors?.length) toast(`${t('Błędy:')} ${r.errors.join(', ')}`, 'warning');
                renderTrashView();
            } catch { toast(t('Błąd przywracania'), 'error'); }
        });

        // Empty trash
        list.querySelector('.fm-trash-empty-btn')?.addEventListener('click', async () => {
            const sure = await confirmDialog(t('Opróżnij kosz'), `${t('Permanently delete')} ${items.length} ${t('items? This cannot be undone.')}`);
            if (!sure) return;
            try {
                const r = await api('/files/trash/empty', { method: 'POST' });
                toast(`${t('Kosz opróżniony')} (${r.removed || 0} ${t('elementów')})`, 'success');
                renderTrashView();
            } catch { toast(t('Błąd opróżniania kosza'), 'error'); }
        });

        // Restore single
        list.querySelectorAll('.fm-trash-restore-one').forEach(btn => {
            btn.addEventListener('click', async () => {
                const tid = btn.dataset.trashId;
                try {
                    const r = await api('/files/trash/restore', { method: 'POST', body: { trash_ids: [tid] } });
                    toast(`${t('Przywrócono:')} ${(r.restored || []).join(', ')}`, 'success');
                    if (r.errors?.length) toast(r.errors.join(', '), 'warning');
                    renderTrashView();
                } catch { toast(t('Błąd przywracania'), 'error'); }
            });
        });

        // Delete single permanently
        list.querySelectorAll('.fm-trash-delete-one').forEach(btn => {
            btn.addEventListener('click', async () => {
                const tid = btn.dataset.trashId;
                const item = items.find(i => i.trash_id === tid);
                const sure = await confirmDialog(t('Usuń trwale'), `${t('Permanently delete')} "${item?.name || ''}"? ${t('This cannot be undone.')}`);
                if (!sure) return;
                try {
                    await api('/files/trash/delete', { method: 'DELETE', body: { trash_ids: [tid] } });
                    toast(t('Trwale usunięto'), 'success');
                    renderTrashView();
                } catch { toast(t('Błąd usuwania'), 'error'); }
            });
        });
    }

    // ─── Shared with me ───

    async function renderSharedWithMe() {
        const list = body.querySelector('#fm-file-list');
        const header = body.querySelector('#fm-list-header');
        if (header) header.style.display = 'none';

        list.className = 'fm-file-list fm-trash-view';
        list.innerHTML = `<div class="fm-empty"><i class="fas fa-spinner fa-spin"></i><span>${t('Ładowanie…')}</span></div>`;

        let shares;
        try {
            shares = await api('/files/shares/received');
        } catch {
            list.innerHTML = `<div class="fm-empty"><i class="fas fa-exclamation-triangle"></i><span>${t('Błąd ładowania')}</span></div>`;
            return;
        }

        if (!shares || !shares.length) {
            list.innerHTML = `<div class="fm-empty"><i class="fas fa-share-alt"></i><span>${t('Nikt Ci jeszcze nic nie udostępnił')}</span></div>`;
            body.querySelector('#fm-statusbar').textContent = t('Brak udostępnień');
            return;
        }

        list.innerHTML = `
            <div class="fm-trash-header">
                <div class="fm-trash-info">
                    <i class="fas fa-share-alt app-icon-share"></i>
                    ${t('Pliki i foldery udostępnione Ci przez innych użytkowników.')}
                </div>
            </div>
            <div class="fm-trash-items">
                ${shares.map(s => {
                    const icon = s.is_dir ? 'fa-folder' : 'fa-file';
                    const expiryText = s.expires ? new Date(s.expires).toLocaleString(getLocale()) : 'Nigdy';
                    return `
                        <div class="fm-trash-item fm-shared-item" data-share-token="${s.token}">
                            <div class="fm-trash-item-icon"><i class="fas ${icon} app-icon-accent"></i></div>
                            <div class="fm-trash-item-info">
                                <div class="fm-trash-item-name">${s.name}</div>
                                <div class="fm-trash-item-meta">
                                    <span><i class="fas fa-user"></i> Od: <strong>${s.creator || '?'}</strong></span>
                                    <span><i class="fas fa-clock"></i> Wygasa: ${expiryText}</span>
                                    <span><i class="fas fa-calendar"></i> ${new Date(s.created).toLocaleString(getLocale())}</span>
                                </div>
                            </div>
                            <div class="fm-trash-item-actions">
                                <button class="fm-trash-btn fm-shared-open" data-share-token="${s.token}" title="${t('Otwórz')}">
                                    <i class="fas fa-external-link-alt"></i>
                                </button>
                            </div>
                        </div>
                    `;
                }).join('')}
            </div>
        `;

        body.querySelector('#fm-statusbar').textContent = `${shares.length} ${t('shares')}`;

        // Open shared item in new tab via share page
        list.querySelectorAll('.fm-shared-open').forEach(btn => {
            btn.addEventListener('click', () => {
                const token = btn.dataset.shareToken;
                window.open(`/share/${token}`, '_blank');
            });
        });

        // Click on item row also opens
        list.querySelectorAll('.fm-shared-item').forEach(el => {
            el.addEventListener('click', (e) => {
                if (e.target.closest('.fm-shared-open')) return;
                const token = el.dataset.shareToken;
                window.open(`/share/${token}`, '_blank');
            });
            el.style.cursor = 'pointer';
        });
    }

    // ─── Navigation ───
    // (Duplicate Photo Finder is now a standalone app — see apps/duplicates.js)

    async function navigateTo(path) {
        // Bump version so any in-flight navigation knows it's stale
        const myVersion = ++state._navVersion;
        console.log(`[FM] navigateTo("${path}") v${myVersion}, current="${state.path}"`);
        // Show loading indicator immediately so user sees response
        const _fmList = body.querySelector('#fm-file-list');
        if (_fmList) _fmList.style.opacity = '0.5';
        // Exit select mode on navigation
        if (state.selectMode) {
            state.selectMode = false;
            const list = body.querySelector('#fm-file-list');
            const btn = body.querySelector('#fm-select-mode-btn');
            if (list) list.classList.remove('fm-select-mode');
            if (btn) { btn.classList.remove('active'); btn.setAttribute('aria-pressed', 'false'); }
        }
        try {
            let data;
            if (path === '/__photo_favorites__') {
                data = await api('/photos/favorites/files');
                if (myVersion !== state._navVersion) return;
            } else if (path === '/__shared_with_me__') {
                state.path = '/__shared_with_me__';
                state.items = [];
                state.selected.clear();
                // Update history
                if (state.historyIndex < state.history.length - 1) {
                    state.history = state.history.slice(0, state.historyIndex + 1);
                }
                state.history.push(path);
                state.historyIndex = state.history.length - 1;
                renderBreadcrumb();
                renderSidebar();
                updateNavButtons();
                await renderSharedWithMe();
                return;
            } else if (path === '/__trash__') {
                state.path = '/__trash__';
                state.items = [];
                state.selected.clear();
                // Update history
                if (state.historyIndex < state.history.length - 1) {
                    state.history = state.history.slice(0, state.historyIndex + 1);
                }
                state.history.push(path);
                state.historyIndex = state.history.length - 1;
                renderBreadcrumb();
                renderSidebar();
                updateNavButtons();
                await renderTrashView();
                return;
            } else {
                // Use hover-prefetched listing if available and fresh (avoids redundant request)
                const _prefetched = _fmPrefetchCache.get(path);
                if (_prefetched && (Date.now() - _prefetched.ts) < _FM_PREFETCH_TTL) {
                    data = _prefetched.data;
                    _fmPrefetchCache.delete(path);  // consume the cached entry
                    console.log(`[FM] navigateTo: using PREFETCH cache for "${path}", ${data.items?.length} items`);
                } else {
                    console.log(`[FM] navigateTo: fetching API for "${path}"`);
                    data = await api(`/files/list?path=${encodeURIComponent(path)}`);
                    console.log(`[FM] navigateTo: API returned path="${data?.path}", ${data?.items?.length} items`);
                    // If a newer navigation started while we were waiting, bail out
                    if (myVersion !== state._navVersion) {
                        console.log(`[FM] navigateTo: STALE v${myVersion} (current v${state._navVersion}), bailing`);
                        return;
                    }
                }
                // Handle locked folder response
                if (data.locked) {
                    const pw = await promptDialog(t('Folder chroniony'), t('Podaj hasło aby otworzyć:'));
                    if (!pw) return;
                    const unlockResult = await api('/files/folder-unlock', { method: 'POST', body: { path: data.protected_path || path, password: pw } });
                    if (unlockResult.ok) {
                        return navigateTo(path);  // retry after unlock
                    }
                    toast(unlockResult.error || t('Nieprawidłowe hasło'), 'error');
                    return;
                }
            }
            const oldPath = state.path;
            state.path = data.path;
            state._lastRealPath = data.path;  // remember for dup scanner
            state.items = data.items;
            state.selected.clear();
            console.log(`[FM] navigateTo: loaded "${data.path}", ${data.items?.length} items (was "${oldPath}")`);

            // Restore focus if going up
            state.focusedIndex = -1;
            if (oldPath && oldPath.startsWith(state.path) && oldPath !== state.path) {
                const rel = oldPath.slice(state.path.length).replace(/^\//, '');
                const folderName = rel.split('/')[0];
                const idx = state.items.findIndex(i => i.name === folderName);
                if (idx >= 0) state.focusedIndex = idx;
            }
            state.lastClickedIndex = state.focusedIndex;

            // Reset pagination, search, dir sizes on navigation
            state.page = 0;
            state.searchResults = null;
            state.dirSizes = {};
            // Stop any previous background dir-size polling
            if (state._dirSizePollTimer) {
                clearTimeout(state._dirSizePollTimer);
                state._dirSizePollTimer = null;
            }
            state._dirSizeJobs = {};
            state._dirSizePollInterval = 1500;
            const si = body.querySelector('#fm-search-input');
            if (si) { si.value = ''; }
            const sc = body.querySelector('#fm-search-clear');
            if (sc) sc.classList.add('hidden');

            // Update history
            if (state.historyIndex < state.history.length - 1) {
                state.history = state.history.slice(0, state.historyIndex + 1);
            }
            state.history.push(path);
            state.historyIndex = state.history.length - 1;

            renderBreadcrumb();
            renderSidebar();
            renderFileList();
            updateNavButtons();
            // Auto-start background dir-size calculation for directories
            startBgDirSizes();
            // Pre-generate thumbnails in background if already in thumb view
            if (state.viewMode === 'thumb') _fmPregenerateThumbs(state.path);
        } catch (err) {
            console.error('[FM] navigateTo error:', err);
            toast(t('Nie można otworzyć folderu'), 'error');
        }
    }

    function updateNavButtons() {
        body.querySelector('#fm-back').disabled = state.historyIndex <= 0;
        body.querySelector('#fm-forward').disabled = state.historyIndex >= state.history.length - 1;
        body.querySelector('#fm-up').disabled = isSpecialPath(state.path) || isAtHomeRoot();
    }

    // ─── Actions ───

    async function createNewFolder() {
        const name = await promptDialog(t('New Folder'), t('Folder name:'), t('New Folder'));
        if (!name) return;
        try {
            await api('/files/mkdir', { method: 'POST', body: { path: joinCurrentPath(name) } });
            toast(`Folder "${name}" utworzony`, 'success');
            navigateTo(state.path);
        } catch {
            toast(t('Błąd tworzenia folderu'), 'error');
        }
    }

    async function createNewFile() {
        const name = await promptDialog('Nowy plik', 'Nazwa pliku:', 'nowy_plik.txt');
        if (!name) return;
        try {
            const blob = new Blob([''], { type: 'text/plain' });
            const formData = new FormData();
            formData.append('files', blob, name);
            formData.append('path', state.path);
            const resp = await fetch('/api/files/upload', {
                method: 'POST',
                headers: { 'Authorization': `Bearer ${NAS.token}` },
                body: formData
            });
            const data = await resp.json();
            if (data.ok || data.uploaded) {
                toast(`Plik "${name}" utworzony`, 'success');
                navigateTo(state.path);
            } else {
                toast(data.error || t('Błąd tworzenia pliku'), 'error');
            }
        } catch {
            toast(t('Błąd tworzenia pliku'), 'error');
        }
    }

    function uploadFiles() {
        const input = document.createElement('input');
        input.type = 'file';
        input.multiple = true;
        input.addEventListener('change', () => {
            if (!input.files.length) return;
            _doUpload(input.files);
        });
        input.click();
    }

    function uploadFolder() {
        const input = document.createElement('input');
        input.type = 'file';
        input.webkitdirectory = true;
        input.multiple = true;
        input.addEventListener('change', () => {
            if (!input.files.length) return;
            _doUploadWithPaths(input.files);
        });
        input.click();
    }

    async function _doUploadWithPaths(files) {
        const filesArr = Array.from(files);
        const total = filesArr.length;
        // Separate large files (need chunked upload) from small ones (batch)
        const largeFiles = filesArr.filter(f => f.size >= _CHUNK_THRESHOLD);
        const smallFiles = filesArr.filter(f => f.size < _CHUNK_THRESHOLD);
        let done = 0;
        let cancelled = false;

        const allDone = () => {
            finishFileOpProgress(true, { channel: 'fm', message: `${t('Przesłano')} ${total} ${t('plik(ów)')}` });
            _fmPrefetchCache.delete(state.path);
            navigateTo(state.path);
        };
        if (largeFiles.length > 0) {
            showFileOpProgress({ operation: 'upload', channel: 'fm', percent: 0, done: 0, total, current_file: largeFiles[0]?.name || '' });
            window._fileopUploadXhr = window._fileopUploadXhr || {};
            window._fileopUploadXhr['fm'] = { abort: () => { cancelled = true; } };

            for (const f of largeFiles) {
                if (cancelled) break;
                // Compute destination directory from relative path (e.g. "Photos/2024/beach.jpg" → destDir includes "Photos/2024")
                const relPath = (f.webkitRelativePath || f.name).replace(/\\/g, '/');
                const parts = relPath.split('/').filter(p => p && p !== '..');
                let destDir = state.path.replace(/\/$/, '');
                if (parts.length > 1) {
                    destDir = destDir + '/' + parts.slice(0, -1).join('/');
                }

                const overallPct = Math.round(done / total * 100);
                showFileOpProgress({ operation: 'upload', channel: 'fm', percent: overallPct, done, total, current_file: f.name });
                const ok = await _doChunkedUpload(f, destDir, (pct) => {
                    const pctNow = Math.min(99, Math.round((done / total + pct / 100 / total) * 100));
                    showFileOpProgress({ operation: 'upload', channel: 'fm', percent: pctNow, done, total, current_file: `${f.name} — ${pct}%` });
                }, () => cancelled, true /* create_dir */);

                if (ok === 'abort') {
                    cancelled = true;
                    delete (window._fileopUploadXhr || {})['fm'];
                    finishFileOpProgress(false, { channel: 'fm', message: t('Przerwano') });
                    return;
                }
                if (ok) done++;
            }
            delete (window._fileopUploadXhr || {})['fm'];
        }

        if (cancelled) return;

        if (smallFiles.length === 0) {
            allDone();
            return;
        }

        // Batch upload small files, preserving relative paths for folder structure
        const smallRelPaths = smallFiles.map(f => f.webkitRelativePath || f.name);
        _doUploadBatch(smallFiles, total, allDone, done, smallRelPaths);
    }

    const _CHUNK_THRESHOLD = 10 * 1024 * 1024; // 10 MB — use chunked upload for larger files
    const _CHUNK_SIZE = 5 * 1024 * 1024;        // 5 MB chunks
    const _CHUNK_MAX_RETRIES = 3;

    function _doUpload(files) {
        const largeFiles = Array.from(files).filter(f => f.size >= _CHUNK_THRESHOLD);
        const smallFiles = Array.from(files).filter(f => f.size < _CHUNK_THRESHOLD);
        const total = files.length;
        let done = 0;

        const allDone = () => {
            finishFileOpProgress(true, { channel: 'fm', message: `${t('Przesłano')} ${total} ${t('plik(ów)')}` });
            _fmPrefetchCache.delete(state.path);
            navigateTo(state.path);
        };

        // Upload all files, chunked for large ones, then regular for small
        if (largeFiles.length === 0) {
            // All small files — single XHR for efficiency
            _doUploadBatch(smallFiles, total, allDone);
            return;
        }

        // Mixed: upload large files one by one (chunked), then batch small ones
        let cancelled = false;

        const uploadNext = async (idx) => {
            if (cancelled) return;
            if (idx >= largeFiles.length) {
                // Done with large files, now batch-upload small ones
                if (smallFiles.length === 0) { allDone(); return; }
                _doUploadBatch(smallFiles, total, allDone, done);
                return;
            }
            const f = largeFiles[idx];
            showFileOpProgress({ operation: 'upload', channel: 'fm', percent: Math.round(done / total * 100), done, total, current_file: f.name });
            const ok = await _doChunkedUpload(f, state.path, (pct) => {
                const overallPct = Math.round((done / total * 100) + pct / total);
                showFileOpProgress({ operation: 'upload', channel: 'fm', percent: Math.min(99, overallPct), done, total, current_file: `${f.name} — ${pct}%` });
            }, () => cancelled);
            if (ok === 'abort') { cancelled = true; finishFileOpProgress(false, { channel: 'fm', message: t('Przerwano') }); return; }
            if (ok) { done++; }
            await uploadNext(idx + 1);
        };

        showFileOpProgress({ operation: 'upload', channel: 'fm', percent: 0, done: 0, total, current_file: largeFiles[0]?.name || '' });
        // Store cancel hook for large file uploads
        window._fileopUploadXhr = window._fileopUploadXhr || {};
        window._fileopUploadXhr['fm'] = { abort: () => { cancelled = true; } };
        uploadNext(0).finally(() => {
            delete (window._fileopUploadXhr || {})['fm'];
        });
    }

    function _doUploadBatch(files, totalOverall, onDone, startDone = 0, relPaths = null) {
        if (files.length === 0) { onDone(); return; }
        const _BATCH_MAX_RETRIES = 2;
        let retriesLeft = _BATCH_MAX_RETRIES;

        const attempt = () => {
            const form = new FormData();
            form.append('path', state.path);
            for (let i = 0; i < files.length; i++) {
                form.append('files', files[i]);
                if (relPaths && relPaths[i]) form.append('rel_paths', relPaths[i]);
            }

            const xhr = new XMLHttpRequest();
            window._fileopUploadXhr = window._fileopUploadXhr || {};
            window._fileopUploadXhr['fm'] = xhr;
            showFileOpProgress({ operation: 'upload', channel: 'fm', percent: Math.round(startDone / totalOverall * 100), done: startDone, total: totalOverall, current_file: files[0]?.name || '' });

            xhr.upload.addEventListener('progress', (e) => {
                if (e.lengthComputable) {
                    const batchPct = e.loaded / e.total;
                    const overallPct = Math.round((startDone + batchPct * files.length) / totalOverall * 100);
                    const doneSoFar = startDone + Math.min(Math.round(batchPct * files.length), files.length);
                    showFileOpProgress({ operation: 'upload', channel: 'fm', percent: Math.min(99, overallPct), done: doneSoFar, total: totalOverall, current_file: `${formatBytes(e.loaded)} / ${formatBytes(e.total)}` });
                }
            });

            xhr.addEventListener('load', () => {
                delete (window._fileopUploadXhr || {})['fm'];
                if (xhr.status >= 200 && xhr.status < 300) {
                    let resp = null;
                    try { resp = JSON.parse(xhr.responseText); } catch {}
                    if (resp && resp.errors && resp.errors.length > 0) {
                        // Partial success: some files uploaded, some failed
                        const uploaded = (resp.uploaded || []).length;
                        const failed = resp.errors.length;
                        if (uploaded > 0) {
                            // Show warning toast for failures but still refresh listing
                            const firstErr = resp.errors[0].error;
                            toast(`${t('Przesłano')} ${uploaded}, ${t('błąd')}: ${firstErr}`, 'warning');
                            _fmPrefetchCache.delete(state.path);
                            onDone();
                        } else {
                            finishFileOpProgress(false, { channel: 'fm', message: resp.errors[0].error || t('Błąd przesyłania') });
                        }
                    } else {
                        onDone();
                    }
                } else if (xhr.status >= 500 && retriesLeft > 0) {
                    retriesLeft--;
                    setTimeout(attempt, 1500 * (_BATCH_MAX_RETRIES - retriesLeft));
                } else {
                    let msg = t('Błąd przesyłania');
                    try { msg = JSON.parse(xhr.responseText).error || msg; } catch {}
                    finishFileOpProgress(false, { channel: 'fm', message: msg });
                }
            });

            xhr.addEventListener('error', () => {
                delete (window._fileopUploadXhr || {})['fm'];
                if (retriesLeft > 0) {
                    retriesLeft--;
                    setTimeout(attempt, 1500 * (_BATCH_MAX_RETRIES - retriesLeft));
                } else {
                    finishFileOpProgress(false, { channel: 'fm', message: t('Błąd sieci') });
                }
            });

            xhr.addEventListener('abort', () => {
                delete (window._fileopUploadXhr || {})['fm'];
                finishFileOpProgress(false, { channel: 'fm', message: t('Przerwano') });
            });

            xhr.open('POST', '/api/files/upload');
            if (NAS.token) xhr.setRequestHeader('Authorization', `Bearer ${NAS.token}`);
            xhr.send(form);
        };

        attempt();
    }

    async function _doChunkedUpload(file, destPath, onProgress, isCancelled, createDir = false) {
        /**
         * Upload a single large file in chunks with retry and resume support.
         * createDir: if true, create destination directory if missing (for folder uploads).
         * Returns true on success, false on error, 'abort' if cancelled.
         */
        const numChunks = Math.max(1, Math.ceil(file.size / _CHUNK_SIZE));
        const MAX_SESSION_RESTARTS = 1; // allow one full restart on session expiry

        async function _initSession() {
            const init = await api('/files/upload-chunk-init', {
                method: 'POST',
                body: { path: destPath, filename: file.name, size: file.size, chunk_size: _CHUNK_SIZE, create_dir: createDir }
            });
            if (init.error) throw new Error(init.error);
            return { sessionId: init.session_id, uploadedChunks: init.uploaded_chunks || [] };
        }

        // Initialize session
        let sessionId;
        let uploadedChunks = [];
        let sessionRestarts = 0;
        try {
            ({ sessionId, uploadedChunks } = await _initSession());
        } catch (e) {
            toast(t('Błąd inicjalizacji przesyłania') + ': ' + (e.message || e), 'error');
            return false;
        }

        // Upload missing chunks
        for (let i = 0; i < numChunks; i++) {
            if (isCancelled && isCancelled()) {
                // Abort session server-side
                try { await api('/files/upload-abort', { method: 'POST', body: { session_id: sessionId } }); } catch {}
                return 'abort';
            }
            if (uploadedChunks.includes(i)) {
                // Already uploaded (resume)
                if (onProgress) onProgress(Math.round((i + 1) / numChunks * 100));
                continue;
            }

            const start = i * _CHUNK_SIZE;
            const end = Math.min(start + _CHUNK_SIZE, file.size);
            const chunk = file.slice(start, end);

            // Compute SHA-256 checksum for integrity verification
            let chunkChecksum = '';
            try {
                const buf = await chunk.arrayBuffer();
                const hash = await crypto.subtle.digest('SHA-256', buf);
                chunkChecksum = Array.from(new Uint8Array(hash)).map(b => b.toString(16).padStart(2, '0')).join('');
            } catch {
                chunkChecksum = ''; // crypto.subtle not available (non-HTTPS) — skip checksum
            }

            let retries = 0;
            let chunkOk = false;
            while (retries < _CHUNK_MAX_RETRIES && !chunkOk) {
                if (isCancelled && isCancelled()) {
                    try { await api('/files/upload-abort', { method: 'POST', body: { session_id: sessionId } }); } catch {}
                    return 'abort';
                }
                try {
                    const form = new FormData();
                    form.append('session_id', sessionId);
                    form.append('chunk_index', i);
                    form.append('chunk', chunk, file.name);
                    if (chunkChecksum) form.append('checksum', chunkChecksum);
                    const res = await new Promise((resolve, reject) => {
                        const xhr = new XMLHttpRequest();
                        window._fileopUploadXhr = window._fileopUploadXhr || {};
                        window._fileopUploadXhr['fm'] = xhr;
                        xhr.addEventListener('load', () => {
                            delete (window._fileopUploadXhr || {})['fm'];
                            try { resolve(JSON.parse(xhr.responseText)); } catch { resolve({ error: 'parse error' }); }
                        });
                        xhr.addEventListener('error', () => {
                            delete (window._fileopUploadXhr || {})['fm'];
                            reject(new Error('network'));
                        });
                        xhr.addEventListener('abort', () => {
                            delete (window._fileopUploadXhr || {})['fm'];
                            reject(new Error('abort'));
                        });
                        xhr.open('POST', '/api/files/upload-chunk');
                        if (NAS.token) xhr.setRequestHeader('Authorization', `Bearer ${NAS.token}`);
                        xhr.send(form);
                    });
                    if (res.ok) {
                        uploadedChunks = res.uploaded_chunks || uploadedChunks;
                        chunkOk = true;
                    } else if (res.expired) {
                        // Session expired (e.g. server restart) — restart with a new session once
                        if (sessionRestarts < MAX_SESSION_RESTARTS) {
                            sessionRestarts++;
                            try {
                                ({ sessionId, uploadedChunks } = await _initSession());
                                i = -1; // restart chunk loop (for-loop will i++ to 0)
                            } catch (e) {
                                toast(t('Błąd inicjalizacji przesyłania') + ': ' + (e.message || e), 'error');
                                return false;
                            }
                            break; // break retry loop, restart outer for-loop
                        }
                        toast(t('Sesja przesyłania wygasła'), 'error');
                        return false;
                    } else {
                        throw new Error(res.error || 'chunk error');
                    }
                } catch (e) {
                    if (e.message === 'abort') return 'abort';
                    retries++;
                    if (retries >= _CHUNK_MAX_RETRIES) {
                        toast(`${t('Błąd przesyłania fragmentu')} ${i + 1}/${numChunks}: ${e.message}`, 'error');
                        try { await api('/files/upload-abort', { method: 'POST', body: { session_id: sessionId } }); } catch {}
                        return false;
                    }
                    // Wait before retry
                    await new Promise(r => setTimeout(r, 1000 * retries));
                }
            }
            if (onProgress) onProgress(Math.round((i + 1) / numChunks * 100));
        }

        // Finalize
        try {
            const fin = await api('/files/upload-complete', { method: 'POST', body: { session_id: sessionId, verify_checksum: true } });
            if (fin.error) {
                if (!fin.missing_chunks) {
                    toast(t('Błąd finalizacji przesyłania') + ': ' + fin.error, 'error');
                }
                return false;
            }
            return true;
        } catch (e) {
            toast(t('Błąd finalizacji przesyłania') + ': ' + (e.message || e), 'error');
            return false;
        }
    }

    async function downloadSelected() {
        const sel = [...state.selected];
        if (!sel.length) { toast('Zaznacz elementy do pobrania', 'warning'); return; }

        // Single regular file — direct download (fast, no zip)
        if (sel.length === 1) {
            const item = state.items.find(i => i.name === sel[0]);
            if (item && !item.is_dir) {
                const path = joinCurrentPath(sel[0]);
                const tk = NAS.token || '';
                const a = document.createElement('a');
                a.href = `/api/files/download?path=${encodeURIComponent(path)}&token=${encodeURIComponent(tk)}`;
                a.download = sel[0];
                a.click();
                return;
            }
        }

        // Multiple items or folder(s) — download as ZIP (async with progress)
        const paths = getSelectedPaths();
        try {
            const r = await api('/files/download-zip', {
                method: 'POST',
                body: { sources: paths }
            });
            if (r.error) { toast(r.error, 'error'); return; }
            // Show initial progress bar immediately (socket.io updates will follow)
            showFileOpProgress({ operation: 'download', channel: 'bg', percent: 0, done: 0, total: 1, current_file: t('Przygotowywanie archiwum…') });
            // Poll for readiness, then trigger browser download
            if (r.download_id) {
                _pollDownloadReady(r.download_id);
            }
        } catch {
            toast(t('Błąd pobierania'), 'error');
        }
    }

    function _pollDownloadReady(downloadId) {
        let attempts = 0;
        const maxAttempts = 300; // 5 minutes max (300 × 1s)
        const iv = setInterval(async () => {
            attempts++;
            if (attempts > maxAttempts) {
                clearInterval(iv);
                finishFileOpProgress(false, { channel: 'bg', message: t('Timeout przygotowania archiwum') });
                return;
            }
            try {
                const r = await api(`/files/download-zip/${downloadId}/status`);
                if (r.ready) {
                    clearInterval(iv);
                    const tk = NAS.token || '';
                    const url = `/api/files/download-zip/${downloadId}?token=${encodeURIComponent(tk)}`;
                    // Use hidden iframe to trigger download — most reliable method
                    const iframe = document.createElement('iframe');
                    iframe.style.display = 'none';
                    iframe.src = url;
                    document.body.appendChild(iframe);
                    setTimeout(() => { try { iframe.remove(); } catch(e) {} }, 120000);
                    toast(`${t('Pobieranie')} ${r.name || 'archiwum'} ${t('rozpoczęte')}`, 'success');
                } else if (r.error) {
                    clearInterval(iv);
                    finishFileOpProgress(false, { channel: 'bg', message: r.error });
                }
            } catch(e) {
                // network error — keep trying
            }
        }, 1000);
    }

    async function deleteSelected() {
        if (!state.selected.size) { toast(t('Zaznacz elementy do usunięcia'), 'warning'); return; }
        const count = state.selected.size;
        const sure = await confirmDialog(t('Przenieś do kosza'), `${t('Move')} ${count} ${t('item(s) to trash?')}`);
        if (!sure) return;

        const paths = [...state.selected].map(name => itemFullPath(name));
        try {
            await api('/files/delete', { method: 'DELETE', body: { paths } });
            toast(t('Moved') + ' ' + count + ' ' + t('item(s) to trash'), 'success');
            navigateTo(state.path);
        } catch {
            toast(t('Błąd przenoszenia do kosza'), 'error');
        }
    }

    async function renameSelected() {
        const name = [...state.selected][0];
        if (!name) { toast('Zaznacz element do zmiany nazwy', 'warning'); return; }
        // Try inline rename first
        if (_startInlineRename(name)) return;
        // Fallback to dialog
        const newName = await promptDialog(t('Zmień nazwę'), t('New name:'), name);
        if (!newName || newName === name) return;
        await _doRename(name, newName);
    }

    async function _doRename(oldName, newName) {
        if (!newName || newName === oldName) return;
        const path = itemFullPath(oldName);
        try {
            await api('/files/rename', { method: 'POST', body: { path, new_name: newName } });
            toast(`${t('Zmieniono nazwę na')} "${newName}"`, 'success');
            navigateTo(state.path);
        } catch {
            toast(t('Błąd zmiany nazwy'), 'error');
        }
    }

    function _startInlineRename(name) {
        const _itemSel = '.fm-file-item, .fm-grid-item, .fm-thumb-item';
        const list = body.querySelector('#fm-file-list');
        const el = [...list.querySelectorAll(_itemSel)].find(e => e.dataset.name === name);
        if (!el) return false;

        // Find the name text container
        const nameEl = el.querySelector('.fm-file-name span, .fm-grid-label, .fm-thumb-label');
        if (!nameEl) return false;

        const origText = nameEl.textContent;
        // For grid/thumb labels, save badges HTML
        const isListView = !!el.querySelector('.fm-file-name');
        let savedBadges = '';
        if (!isListView) {
            // Extract badges (spans) from the label
            savedBadges = nameEl.innerHTML.replace(origText, '');
        }

        const input = document.createElement('input');
        input.type = 'text';
        input.value = name;
        input.className = 'fm-inline-rename';

        // Select filename without extension for files
        const item = state.items.find(i => i.name === name);
        if (item && !item.is_dir) {
            const dotIdx = name.lastIndexOf('.');
            if (dotIdx > 0) {
                setTimeout(() => input.setSelectionRange(0, dotIdx), 0);
            }
        }

        if (isListView) {
            nameEl.textContent = '';
            nameEl.appendChild(input);
        } else {
            nameEl.innerHTML = '';
            nameEl.appendChild(input);
        }
        input.focus();
        if (!item || item.is_dir) input.select();

        let committed = false;
        const commit = () => {
            if (committed) return;
            committed = true;
            const newName = input.value.trim();
            // Restore original text
            if (isListView) {
                nameEl.textContent = newName || origText;
            } else {
                nameEl.innerHTML = (newName || origText) + savedBadges;
            }
            if (newName && newName !== name) {
                _doRename(name, newName);
            }
        };

        input.addEventListener('blur', commit);
        input.addEventListener('keydown', (e) => {
            e.stopPropagation();
            if (e.key === 'Enter') { e.preventDefault(); input.blur(); }
            if (e.key === 'Escape') { e.preventDefault(); input.value = name; input.blur(); }
        });
        return true;
    }

    // ─── Clipboard (Copy/Cut/Paste) ───

    function getSelectedPaths() {
        return [...state.selected].map(name => itemFullPath(name));
    }

    function clipboardCopy() {
        if (!state.selected.size) { toast('Zaznacz elementy', 'warning'); return; }
        state.clipboard = { mode: 'copy', paths: getSelectedPaths(), basePath: state.path };
        toast(t('Copied') + ' ' + state.clipboard.paths.length + ' ' + t('item(s)') + ' ' + t('to clipboard'), 'info');
        updateClipboardBar();
    }

    function clipboardCut() {
        if (!state.selected.size) { toast('Zaznacz elementy', 'warning'); return; }
        state.clipboard = { mode: 'cut', paths: getSelectedPaths(), basePath: state.path };
        toast(t('Cut') + ' ' + state.clipboard.paths.length + ' ' + t('item(s)'), 'info');
        updateClipboardBar();
    }

    async function clipboardPaste() {
        if (!state.clipboard) { toast('Schowek jest pusty', 'warning'); return; }
        const { mode, paths } = state.clipboard;
        const dest = state.path;

        // Check for conflicts first
        let onConflict = 'rename';
        try {
            const check = await api('/files/check-conflicts', { method: 'POST', body: { sources: paths, dest } });
            if (check.conflicts && check.conflicts.length > 0) {
                onConflict = await showConflictDialog(check.conflicts, mode);
                if (onConflict === null) return; // cancelled
            }
        } catch { /* proceed without conflict check */ }

        try {
            if (mode === 'copy') {
                const r = await api('/files/copy', { method: 'POST', body: { sources: paths, dest, on_conflict: onConflict } });
                if (r.error) { toast(r.error, 'error'); return; }
                if (r.async) {
                    toast(r.message || 'Kopiowanie w tle…', 'info');
                    state.clipboard = null;
                    updateClipboardBar();
                    return;
                }
                const count = (r.copied || []).length;
                const skipCount = (r.skipped || []).length;
                let msg = t('Copied') + ' ' + count + ' ' + t('item(s)');
                if (skipCount) msg += `, ${t('pominięto')} ${skipCount}`;
                toast(msg, 'success');
                if (r.errors && r.errors.length) toast(r.errors.join('; '), 'warning');
            } else {
                const r = await api('/files/move-multi', { method: 'POST', body: { sources: paths, dest, on_conflict: onConflict } });
                if (r.error) { toast(r.error, 'error'); return; }
                if (r.async) {
                    toast(r.message || 'Przenoszenie w tle…', 'info');
                    state.clipboard = null;
                    updateClipboardBar();
                    return;
                }
                const count = (r.moved || []).length;
                const skipCount = (r.skipped || []).length;
                let msg = t('Moved') + ' ' + count + ' ' + t('item(s)');
                if (skipCount) msg += `, ${t('pominięto')} ${skipCount}`;
                toast(msg, 'success');
                if (r.errors && r.errors.length) toast(r.errors.join('; '), 'warning');
                state.clipboard = null;
            }
            navigateTo(state.path);
        } catch (e) {
            toast(t('Błąd operacji') + (e?.message ? ': ' + e.message : ''), 'error');
        }
    }

    function showConflictDialog(conflicts, mode) {
        const modeLabel = mode === 'copy' ? 'kopiowaniu' : 'przenoszeniu';
        return new Promise((resolve) => {
            const overlay = document.createElement('div');
            overlay.className = 'modal-overlay';
            const listHtml = conflicts.map(c => {
                const icon = c.is_dir ? 'fa-folder' : 'fa-file';
                return `<div class="app-list-row">
                    <i class="fas ${icon} app-icon-fixed app-icon-accent"></i>
                    <span class="app-filename">${c.name}</span>
                </div>`;
            }).join('');

            overlay.innerHTML = `
                <div class="modal app-modal-md">
                    <div class="modal-header"><i class="fas fa-exclamation-triangle app-hdr-icon app-hdr-icon--warning"></i>Konflikty przy ${modeLabel}</div>
                    <div class="modal-body">
                        <div class="app-desc">
                            ${conflicts.length === 1 ? t('Poniższy element już istnieje') : `${t('Poniższe')} ${conflicts.length} ${t('elementy już istnieją')}`} w docelowym folderze:
                        </div>
                        <div class="app-scroll-box">
                            ${listHtml}
                        </div>
                        <div class="app-sublabel">${t('Co chcesz zrobić?')}</div>
                    </div>
                    <div class="modal-footer app-row-wrap">
                        <button class="btn app-mr-auto" id="conflict-cancel">Anuluj</button>
                        <button class="btn" id="conflict-skip" title="${t('Nie kopiuj istniejących')}"><i class="fas fa-forward"></i> ${t('Pomiń')}</button>
                        <button class="btn" id="conflict-rename" title="${t('Zachowaj oba z nową nazwą')}"><i class="fas fa-clone"></i> ${t('Zachowaj oba')}</button>
                        <button class="btn btn-primary" id="conflict-overwrite" title="${t('Zastąp istniejące')}"><i class="fas fa-sync-alt"></i> ${t('Nadpisz')}</button>
                    </div>
                </div>
            `;
            document.body.appendChild(overlay);
            overlay.querySelector('#conflict-cancel').addEventListener('click', () => { overlay.remove(); resolve(null); });
            overlay.querySelector('#conflict-skip').addEventListener('click', () => { overlay.remove(); resolve('skip'); });
            overlay.querySelector('#conflict-rename').addEventListener('click', () => { overlay.remove(); resolve('rename'); });
            overlay.querySelector('#conflict-overwrite').addEventListener('click', () => { overlay.remove(); resolve('overwrite'); });
            overlay.addEventListener('click', (e) => { if (e.target === overlay) { overlay.remove(); resolve(null); } });
        });
    }

    // ─── Compress / Extract ───

    async function compressSelected(format) {
        const paths = getSelectedPaths();
        if (!paths.length) { toast('Zaznacz elementy do kompresji', 'warning'); return; }

        const ext = format === 'zip' ? '.zip' : '.tar.gz';

        // Suggest a name
        let suggestion = '';
        const sel = [...state.selected];
        if (sel.length === 1) {
            // Single item — use its name without extension
            const n = sel[0];
            const dotIdx = n.lastIndexOf('.');
            suggestion = (dotIdx > 0 ? n.substring(0, dotIdx) : n);
        } else {
            // Multiple items — use current folder name
            const parts = state.path.split('/').filter(Boolean);
            suggestion = parts.length ? parts[parts.length - 1] : 'archiwum';
        }

        // Show name dialog
        const archiveName = await new Promise((resolve) => {
            const overlay = document.createElement('div');
            overlay.className = 'modal-overlay';
            overlay.innerHTML = `
                <div class="modal">
                    <div class="modal-header"><i class="fas fa-file-archive app-hdr-icon"></i>Kompresuj do ${ext}</div>
                    <div class="modal-body">
                        <div class="app-note">
                            ${sel.length} element${sel.length > 1 ? t('ów') : ''} ${t('selected')}
                        </div>
                        <label class="modal-label">Nazwa archiwum:</label>
                        <div class="app-input-group">
                            <input class="modal-input app-input-prefix" id="compress-name-input" value="${suggestion}">
                            <span class="app-input-suffix">${ext}</span>
                        </div>
                    </div>
                    <div class="modal-footer">
                        <button class="btn" id="compress-dlg-cancel">Anuluj</button>
                        <button class="btn btn-primary" id="compress-dlg-ok"><i class="fas fa-compress app-btn-icon"></i>Kompresuj</button>
                    </div>
                </div>
            `;
            document.body.appendChild(overlay);
            const inp = overlay.querySelector('#compress-name-input');
            inp.focus();
            inp.select();

            const submit = () => {
                const val = inp.value.trim();
                overlay.remove();
                resolve(val || null);
            };
            inp.addEventListener('keydown', (e) => { if (e.key === 'Enter') submit(); if (e.key === 'Escape') { overlay.remove(); resolve(null); } });
            overlay.querySelector('#compress-dlg-cancel').addEventListener('click', () => { overlay.remove(); resolve(null); });
            overlay.querySelector('#compress-dlg-ok').addEventListener('click', submit);
            overlay.addEventListener('click', (e) => { if (e.target === overlay) { overlay.remove(); resolve(null); } });
        });

        if (!archiveName) return;

        try {
            const r = await api('/files/compress', {
                method: 'POST',
                body: { sources: paths, format: format, name: archiveName }
            });
            if (r.error) { toast(r.error, 'error'); return; }
            toast(r.message || 'Kompresja w tle…', 'info');
        } catch {
            toast(t('Błąd kompresji'), 'error');
        }
    }

    async function extractSelected() {
        const sel = [...state.selected];
        if (sel.length !== 1) { toast('Zaznacz jeden plik archiwum', 'warning'); return; }
        const archivePath = joinCurrentPath(sel[0]);
        try {
            const r = await api('/files/extract', {
                method: 'POST',
                body: { path: archivePath }
            });
            if (r.error) { toast(r.error, 'error'); return; }
            toast(r.message || 'Rozpakowywanie w tle…', 'info');
        } catch {
            toast(t('Błąd rozpakowywania'), 'error');
        }
    }

    // ─── Logs Dialog ──────────────────────────────────────────────────────────

    async function showLogsDialog(path) {
        const overlay = document.createElement('div');
        overlay.className = 'app-modal-overlay';
        overlay.style.cssText = 'position:fixed;inset:0;background:rgba(0,0,0,0.6);z-index:9999;display:flex;align-items:center;justify-content:center';

        const dialog = document.createElement('div');
        dialog.className = 'app-modal';
        dialog.style.cssText = 'background:var(--bg-surface,#1e1e2e);border:1px solid var(--border,#333);border-radius:12px;padding:20px;width:700px;max-width:95vw;height:80vh;display:flex;flex-direction:column;box-shadow:0 8px 32px rgba(0,0,0,0.5)';

        dialog.innerHTML = `
            <div style="display:flex;justify-content:space-between;align-items:center;margin-bottom:16px">
                <h3 style="margin:0;font-size:1.2em"><i class="fas fa-history"></i> ${t('Dziennik zdarzeń plików')}</h3>
                <button class="app-btn-icon" id="fm-logs-close"><i class="fas fa-times"></i></button>
            </div>
            <div style="display:flex;gap:10px;margin-bottom:12px">
                <input type="text" id="fm-logs-search" placeholder="${t('Szukaj...')}" style="flex:1;padding:8px;border-radius:6px;border:1px solid var(--border,#444);background:var(--bg-base,#181825);color:var(--text-primary)">
                <select id="fm-logs-filter" class="fm-input" title="${t('Kategoria logów')}">
                    <option value="">${t('Wszystkie')}</option>
                    <option value="files" selected>${t('Operacje plików')}</option>
                    <option value="security">${t('Bezpieczeństwo')}</option>
                    <option value="error">${t('Błędy')}</option>
                </select>
                <button id="fm-logs-refresh" class="app-btn"><i class="fas fa-sync-alt"></i></button>
            </div>
            <div id="fm-logs-list" style="flex:1;overflow-y:auto;border:1px solid var(--border,#333);border-radius:6px;background:var(--bg-base,#0f0f15);padding:8px;font-family:monospace;font-size:0.9em">
                <div style="padding:20px;text-align:center;color:var(--text-muted)">${t('Ładowanie...')}</div>
            </div>
        `;

        overlay.appendChild(dialog);
        document.body.appendChild(overlay);

        const close = () => overlay.remove();
        overlay.querySelector('#fm-logs-close').onclick = close;
        overlay.onclick = e => { if (e.target === overlay) close(); };

        const listEl = overlay.querySelector('#fm-logs-list');
        const searchInput = overlay.querySelector('#fm-logs-search');
        const filterSelect = overlay.querySelector('#fm-logs-filter');
        const refreshBtn = overlay.querySelector('#fm-logs-refresh');

        async function loadLogs() {
            listEl.innerHTML = `<div style="padding:20px;text-align:center;color:var(--text-muted)">${t('Ładowanie...')}</div>`;
            try {
                const search = searchInput.value.trim();
                const category = filterSelect.value;
                const url = `/api/eventlog?limit=100&category=${category}&search=${encodeURIComponent(search)}`;
                const res = await api(url);
                if (!res.events || !res.events.length) {
                    listEl.innerHTML = `<div style="padding:20px;text-align:center;color:var(--text-muted)">${t('Brak zdarzeń')}</div>`;
                    return;
                }

                listEl.innerHTML = res.events.map(e => {
                    const time = e.time || new Date(e.ts * 1000).toLocaleString();
                    const color = e.level === 'error' ? '#ef4444' : e.level === 'warning' ? '#eab308' : '#a6accd';
                    const icon = e.level === 'error' ? 'fa-exclamation-circle' : e.level === 'warning' ? 'fa-exclamation-triangle' : 'fa-info-circle';
                    return `
                        <div style="display:flex;gap:10px;padding:8px;border-bottom:1px solid var(--border,#222);align-items:start">
                            <div style="color:var(--text-muted);white-space:nowrap;width:140px;font-size:0.85em">${time}</div>
                            <div style="color:${color};width:20px;text-align:center"><i class="fas ${icon}"></i></div>
                            <div style="flex:1;word-break:break-word">
                                <div style="font-weight:500">${escapeHtml(e.message)}</div>
                                ${e.details ? `<div style="font-size:0.85em;color:var(--text-muted);margin-top:4px;white-space:pre-wrap">${escapeHtml(JSON.stringify(e.details, null, 2))}</div>` : ''}
                            </div>
                        </div>
                    `;
                }).join('');
            } catch (e) {
                listEl.innerHTML = `<div style="padding:20px;text-align:center;color:var(--danger)">${t('Błąd ładowania logów')}</div>`;
            }
        }

        refreshBtn.onclick = loadLogs;
        searchInput.onkeydown = e => { if(e.key === 'Enter') loadLogs(); };
        filterSelect.onchange = loadLogs;

        loadLogs();
    }

    // ─── Transfer to remote NAS ───


    // ─── File Properties Dialog ───────────────────────────────────────────────

    async function showPropertiesDialog(item, itemPath) {
        const isAdmin = NAS.user?.role === 'admin';
        let perms = {};
        try {
            perms = await api(`/files/permissions?path=${encodeURIComponent(itemPath)}`);
        } catch (e) {
            perms = {
                permissions: item.permissions || '',
                permissions_symbolic: item.permissions_symbolic || '',
                owner: item.owner || '',
                group: item.group || '',
            };
        }

        const overlay = document.createElement('div');
        overlay.className = 'app-modal-overlay';
        overlay.style.cssText = 'position:fixed;inset:0;background:rgba(0,0,0,0.6);z-index:9999;display:flex;align-items:center;justify-content:center';

        const dialog = document.createElement('div');
        dialog.className = 'app-modal';
        dialog.style.cssText = 'background:var(--bg-surface,#1e1e2e);border:1px solid var(--border,#333);border-radius:12px;padding:24px;min-width:420px;max-width:500px;width:90vw;box-shadow:0 8px 32px rgba(0,0,0,0.5)';

        const symPerm = perms.permissions_symbolic || item.permissions_symbolic || '';
        const octPerm = perms.permissions || item.permissions || '';
        const owner = perms.owner || item.owner || '—';
        const group = perms.group || item.group || '—';

        // Helper to generate checkboxes for permissions
        const getPermChecks = (oct) => {
            const p = parseInt(oct || '0', 8);
            const check = (mask) => (p & mask) ? 'checked' : '';
            return `
                <div style="display:grid;grid-template-columns:1fr 1fr 1fr;gap:8px;text-align:center;margin:10px 0;font-size:0.9em">
                    <div></div><div>R</div><div>W</div><div>X</div>
                    <div style="text-align:left">${t('Właściciel')}</div>
                    <div><input type="checkbox" class="perm-cb" data-val="400" ${check(0o400)} ${!isAdmin ? 'disabled' : ''}></div>
                    <div><input type="checkbox" class="perm-cb" data-val="200" ${check(0o200)} ${!isAdmin ? 'disabled' : ''}></div>
                    <div><input type="checkbox" class="perm-cb" data-val="100" ${check(0o100)} ${!isAdmin ? 'disabled' : ''}></div>

                    <div style="text-align:left">${t('Grupa')}</div>
                    <div><input type="checkbox" class="perm-cb" data-val="40" ${check(0o040)} ${!isAdmin ? 'disabled' : ''}></div>
                    <div><input type="checkbox" class="perm-cb" data-val="20" ${check(0o020)} ${!isAdmin ? 'disabled' : ''}></div>
                    <div><input type="checkbox" class="perm-cb" data-val="10" ${check(0o010)} ${!isAdmin ? 'disabled' : ''}></div>

                    <div style="text-align:left">${t('Inni')}</div>
                    <div><input type="checkbox" class="perm-cb" data-val="4" ${check(0o004)} ${!isAdmin ? 'disabled' : ''}></div>
                    <div><input type="checkbox" class="perm-cb" data-val="2" ${check(0o002)} ${!isAdmin ? 'disabled' : ''}></div>
                    <div><input type="checkbox" class="perm-cb" data-val="1" ${check(0o001)} ${!isAdmin ? 'disabled' : ''}></div>
                </div>
            `;
        };

        const chmodSection = `
            <div style="margin-top:16px;padding-top:16px;border-top:1px solid var(--border,#333)">
                <div style="font-weight:600;margin-bottom:8px;color:var(--text-primary)">${t('Uprawnienia')} ${!isAdmin ? '<span style="font-size:0.75em;color:var(--text-muted)">(tylko do odczytu)</span>' : ''}</div>
                ${getPermChecks(octPerm)}
                <div style="display:flex;align-items:center;gap:10px;margin-top:10px">
                    <span style="font-size:0.9em;color:var(--text-muted)">Octal:</span>
                    <input id="fm-chmod-input" type="text" value="${octPerm}" maxlength="4" pattern="[0-7]{3,4}"
                        style="width:60px;padding:4px 8px;background:var(--bg-base,#181825);border:1px solid var(--border,#444);border-radius:4px;color:var(--text-primary);font-family:monospace" ${!isAdmin ? 'disabled' : ''}>
                </div>
            </div>
        `;

        const chownSection = isAdmin ? `
            <div style="margin-top:16px;padding-top:16px;border-top:1px solid var(--border,#333)">
                 <div style="font-weight:600;margin-bottom:8px;color:var(--text-primary)">${t('Właściciel')}</div>
                 <div style="display:grid;grid-template-columns:1fr 1fr;gap:10px">
                    <div>
                        <label style="font-size:0.8em;color:var(--text-muted)">User</label>
                        <input id="fm-chown-user" type="text" value="${owner}" style="width:100%;padding:6px;background:var(--bg-base,#181825);border:1px solid var(--border,#444);border-radius:4px;color:var(--text-primary)">
                    </div>
                    <div>
                        <label style="font-size:0.8em;color:var(--text-muted)">Group</label>
                        <input id="fm-chown-group" type="text" value="${group}" style="width:100%;padding:6px;background:var(--bg-base,#181825);border:1px solid var(--border,#444);border-radius:4px;color:var(--text-primary)">
                    </div>
                 </div>
            </div>
        ` : '';

        dialog.innerHTML = `
            <div style="display:flex;align-items:center;gap:12px;margin-bottom:20px">
                <i class="fas ${item.is_dir ? 'fa-folder' : 'fa-file'}" style="font-size:1.5em;color:var(--accent)"></i>
                <div style="font-size:1.1em;font-weight:600;word-break:break-all">${item.name}</div>
            </div>
            <table style="width:100%;border-collapse:collapse;font-size:0.92em">
                <tr><td style="color:var(--text-muted);padding:4px 0;width:120px">${t('Typ')}</td><td style="color:var(--text-primary)">${item.is_dir ? t('Folder') : t('Plik')}</td></tr>
                <tr><td style="color:var(--text-muted);padding:4px 0">${t('Rozmiar')}</td><td style="color:var(--text-primary)">${typeof fmtBytes !== 'undefined' ? fmtBytes(item.size) : item.size + ' B'}</td></tr>
                <tr><td style="color:var(--text-muted);padding:4px 0">${t('Zmieniony')}</td><td style="color:var(--text-primary)">${formatDate(item.modified)}</td></tr>
                ${!isAdmin ? `<tr><td style="color:var(--text-muted);padding:4px 0">${t('Właściciel')}</td><td style="color:var(--text-primary)">${owner}:${group}</td></tr>` : ''}
                ${item.protected ? `<tr><td style="color:var(--text-muted);padding:4px 0">${t('Ochrona')}</td><td style="color:${item.locked ? 'var(--danger)' : 'var(--success,#22c55e)'}"><i class="fas ${item.locked ? 'fa-lock' : 'fa-lock-open'}"></i> ${item.locked ? t('Zablokowany') : t('Odblokowany')}</td></tr>` : ''}
            </table>
            ${chmodSection}
            ${chownSection}
            <div style="margin-top:20px;text-align:right;display:flex;gap:10px;justify-content:flex-end">
                <button id="fm-props-close" class="app-btn" style="padding:8px 20px">${t('Zamknij')}</button>
                ${isAdmin ? `<button id="fm-props-save" class="app-btn app-btn-primary" style="padding:8px 20px">${t('Zapisz')}</button>` : ''}
            </div>
        `;

        overlay.appendChild(dialog);
        document.body.appendChild(overlay);

        const close = () => overlay.remove();
        overlay.querySelector('#fm-props-close').addEventListener('click', close);
        overlay.addEventListener('click', e => { if (e.target === overlay) close(); });

        if (isAdmin) {
            const checkboxes = overlay.querySelectorAll('.perm-cb');
            const octInput = overlay.querySelector('#fm-chmod-input');

            const updateOct = () => {
                let oct = 0;
                checkboxes.forEach(cb => {
                    if (cb.checked) oct += parseInt(cb.dataset.val, 8);
                });
                octInput.value = '0' + oct.toString(8);
            };

            checkboxes.forEach(cb => cb.addEventListener('change', updateOct));

            octInput.addEventListener('input', () => {
                let val = parseInt(octInput.value, 8);
                if (isNaN(val)) return;
                checkboxes.forEach(cb => {
                    const mask = parseInt(cb.dataset.val, 8);
                    cb.checked = (val & mask) !== 0;
                });
            });

            overlay.querySelector('#fm-props-save').addEventListener('click', async () => {
                const modeVal = octInput.value.trim();
                const newUser = overlay.querySelector('#fm-chown-user')?.value.trim();
                const newGroup = overlay.querySelector('#fm-chown-group')?.value.trim();

                let success = true;

                // chmod
                if (modeVal !== octPerm) {
                     const r = await api('/files/chmod', { method: 'POST', body: { path: itemPath, mode: modeVal } });
                     if (!r.ok && !r.permissions) { toast(r.error || t('Błąd uprawnień'), 'error'); success = false; }
                }

                // chown
                if (newUser !== owner || newGroup !== group) {
                     const r = await api('/files/chown', { method: 'POST', body: { path: itemPath, owner: newUser, group: newGroup } });
                     if (r.error) { toast(r.error || t('Błąd właściciela'), 'error'); success = false; }
                }

                if (success) {
                    toast(t('Zapisano zmiany'), 'success');
                    close();
                    if (state.path) renderFileList(); // refresh list
                }
            });
        }
    }

    async function transferToRemoteNAS() {
        const paths = getSelectedPaths();
        if (!paths.length) { toast('Zaznacz elementy do transferu', 'warning'); return; }

        // Fetch available servers
        let servers = [];
        try {
            const r = await api('/files/remote-servers');
            servers = r.servers || [];
        } catch (e) {
            toast(t('Nie udało się załadować listy serwerów'), 'error'); return;
        }
        if (!servers.length) {
            toast(t('Brak skonfigurowanych serwerów. Dodaj serwer w Ustawieniach > Zdalne serwery.'), 'warning');
            return;
        }

        // Build transfer dialog
        const selNames = [...state.selected];
        const selLabel = selNames.length > 3
            ? `${selNames.slice(0, 3).join(', ')} +${selNames.length - 3} ${t('więcej')}`
            : selNames.join(', ');

        const result = await new Promise((resolve) => {
            const overlay = document.createElement('div');
            overlay.className = 'modal-overlay';
            overlay.innerHTML = `
                <div class="modal app-modal-md">
                    <div class="modal-header">
                        <i class="fas fa-cloud-upload-alt app-hdr-icon"></i>Transferuj do NAS
                    </div>
                    <div class="modal-body">
                        <div class="app-info-box">
                            <div class="app-sublabel">Zaznaczone pliki:</div>
                            <div class="app-filename">${selLabel}</div>
                        </div>
                        <label class="modal-label">Serwer docelowy:</label>
                        <select class="modal-input app-mb-md" id="transfer-server">
                            ${servers.map(s => `<option value="${s.id}">${s.name} (${s.host})</option>`).join('')}
                        </select>
                        <label class="modal-label">${t('Ścieżka zdalna:')}</label>
                        <input class="modal-input" id="transfer-remote-path" value="${servers[0]?.remote_path || '~/'}" placeholder="np. ~/received">
                        <div class="app-hint app-mt-xs">
                            Folder docelowy na zdalnym serwerze. Zostanie utworzony automatycznie.
                        </div>
                    </div>
                    <div class="modal-footer">
                        <button class="btn" id="transfer-cancel">Anuluj</button>
                        <button class="btn btn-primary" id="transfer-start">
                            <i class="fas fa-paper-plane app-btn-icon"></i>Transferuj
                        </button>
                    </div>
                </div>
            `;
            document.body.appendChild(overlay);

            const serverSelect = overlay.querySelector('#transfer-server');
            const remotePath = overlay.querySelector('#transfer-remote-path');

            // Update remote path when server changes
            serverSelect.addEventListener('change', () => {
                const srv = servers.find(s => s.id === serverSelect.value);
                if (srv) remotePath.value = srv.remote_path || '~/';
            });

            const submit = () => {
                const val = { server_id: serverSelect.value, remote_path: remotePath.value.trim() || '~/' };
                overlay.remove();
                resolve(val);
            };

            remotePath.addEventListener('keydown', (e) => { if (e.key === 'Enter') submit(); if (e.key === 'Escape') { overlay.remove(); resolve(null); } });
            overlay.querySelector('#transfer-cancel').addEventListener('click', () => { overlay.remove(); resolve(null); });
            overlay.querySelector('#transfer-start').addEventListener('click', submit);
            overlay.addEventListener('click', (e) => { if (e.target === overlay) { overlay.remove(); resolve(null); } });

            serverSelect.focus();
        });

        if (!result) return;

        try {
            const r = await api('/files/transfer-remote', {
                method: 'POST',
                body: { server_id: result.server_id, paths, remote_path: result.remote_path }
            });
            if (r.error) { toast(r.error, 'error'); return; }
            toast(r.message || t('Transfer rozpoczęty…'), 'info');
        } catch (e) {
            toast(t('Błąd transferu: ') + (e.message || e), 'error');
        }
    }

    // ─── SocketIO file operation listeners ───

    function _initFileOpListeners() {
        if (!window.NAS || !NAS.socket) return;
        NAS.socket.on('fileop_complete', (data) => {
            const op = data.operation || '';
            if (op === 'download') return; // handled by fileop_download_ready
            const labels = { copy: 'Kopiowanie', move: 'Przenoszenie', compress: 'Kompresja', extract: 'Rozpakowywanie', transfer: 'Transfer do NAS' };
            toast(`${labels[op] || t('Operacja')} ${t('zakończona:')} ${data.message || ''}`, 'success');
            navigateTo(state.path);
        });
        NAS.socket.on('fileop_error', (data) => {
            const op = data.operation || '';
            const labels = { copy: 'Kopiowanie', move: 'Przenoszenie', compress: 'Kompresja', extract: 'Rozpakowywanie', download: 'Pobieranie ZIP', transfer: 'Transfer do NAS' };
            toast(`${labels[op] || t('Operacja')} — ${t('błąd:')} ${data.message || ''}`, 'error');
            navigateTo(state.path);
        });
        NAS.socket.on('fileop_download_ready', (data) => {
            // Notification only — actual download triggered by polling in downloadSelected
            toast(`Archiwum ${data.name || ''} gotowe do pobrania`, 'success');
        });
    }

    // Attach listeners when app is opened
    _initFileOpListeners();

    async function shareLink() {
        const name = [...state.selected][0];
        if (!name) return;
        const fullPath = itemFullPath(name);

        // Fetch user list for "share with user" option
        let allUsers = [];
        try {
            const ulist = await api('/users/list');
            const me = (typeof NAS !== 'undefined' && NAS.user) ? NAS.user.username : '';
            allUsers = (ulist || []).filter(u => u.nasos_user && u.username !== me);
        } catch(e) {}

        const result = await new Promise((resolve) => {
            const overlay = document.createElement('div');
            overlay.className = 'modal-overlay';
            overlay.innerHTML = `
                <div class="modal">
                    <div class="modal-header"><i class="fas fa-link app-hdr-icon"></i>${t('Udostępnij przez link')}</div>
                    <div class="modal-body">
                        <div class="app-mb-md">
                            <span class="app-label-muted">Plik/folder:</span><br>
                            <code class="app-path">${fullPath}</code>
                        </div>
                        <label class="modal-label">${t('Wygaśnięcie:')}</label>
                        <select class="modal-input" id="share-link-expiry">
                            <option value="0">Nigdy</option>
                            <option value="1">1 godzina</option>
                            <option value="24" selected>24 godziny</option>
                            <option value="168">7 dni</option>
                            <option value="720">30 dni</option>
                        </select>
                        ${allUsers.length ? `
                        <div class="app-mt-md">
                            <label class="modal-label app-label-row">
                                <input type="checkbox" id="share-link-user-toggle"> ${t('Udostępnij konkretnemu użytkownikowi')}
                            </label>
                            <div id="share-link-users" class="app-user-list hidden">
                                ${allUsers.map(u => `
                                    <label class="app-check-label">
                                        <input type="checkbox" class="share-user-cb" value="${u.username}">
                                        <i class="fas fa-user app-icon-muted-sm"></i> ${u.username}
                                    </label>
                                `).join('')}
                            </div>
                        </div>` : ''}
                    </div>
                    <div class="modal-footer">
                        <button class="btn" id="share-link-cancel">Anuluj</button>
                        <button class="btn btn-primary" id="share-link-ok"><i class="fas fa-link"></i> ${t('Utwórz link')}</button>
                    </div>
                </div>
            `;
            document.body.appendChild(overlay);

            // Toggle user list visibility
            const toggle = overlay.querySelector('#share-link-user-toggle');
            const userList = overlay.querySelector('#share-link-users');
            if (toggle && userList) {
                toggle.addEventListener('change', () => { userList.style.display = toggle.checked ? 'block' : 'none'; });
            }

            overlay.querySelector('#share-link-cancel').addEventListener('click', () => { overlay.remove(); resolve(null); });
            overlay.querySelector('#share-link-ok').addEventListener('click', () => {
                const hours = overlay.querySelector('#share-link-expiry').value;
                const selectedUsers = [];
                if (toggle && toggle.checked) {
                    overlay.querySelectorAll('.share-user-cb:checked').forEach(cb => selectedUsers.push(cb.value));
                }
                overlay.remove();
                resolve({ hours, shared_with: selectedUsers });
            });
            overlay.addEventListener('click', (e) => { if (e.target === overlay) { overlay.remove(); resolve(null); } });
        });

        if (result === null) return;

        try {
            const body = { path: fullPath, expires_hours: parseInt(result.hours) };
            if (result.shared_with && result.shared_with.length > 0) {
                body.shared_with = result.shared_with;
            }
            const share = await api('/files/shares', {
                method: 'POST',
                body
            });
            if (share.error) { toast(t('Błąd: ') + share.error, 'error'); return; }

            const shareUrl = `${window.location.origin}/share/${share.token}`;
            const isUserShare = result.shared_with && result.shared_with.length > 0;

            // Show result modal with copy button
            const overlay2 = document.createElement('div');
            overlay2.className = 'modal-overlay';
            overlay2.innerHTML = `
                <div class="modal">
                    <div class="modal-header"><i class="fas fa-check-circle app-hdr-icon app-hdr-icon--success"></i>Link utworzony</div>
                    <div class="modal-body">
                        <div class="app-note">${t('Udostępniono:')} <strong>${name}</strong></div>
                        ${isUserShare ? `<div class="app-note app-note--accent"><i class="fas fa-users"></i> Dla: ${result.shared_with.join(', ')}</div>` : ''}
                        <div class="app-row">
                            <input class="modal-input app-mono-input" id="share-link-url" value="${shareUrl}" readonly>
                            <button class="btn btn-primary app-nowrap" id="share-link-copy"><i class="fas fa-copy"></i> ${t('Copy')}</button>
                        </div>
                        ${parseInt(result.hours) > 0 ? `<div class="app-hint"><i class="fas fa-clock"></i> Wygasa za ${result.hours === '1' ? t('1 godzinę') : result.hours + ' godz.'}</div>` : '<div class="app-hint"><i class="fas fa-infinity"></i> Link nie wygasa</div>'}
                        ${isUserShare ? `<div class="app-hint app-mt-xs"><i class="fas fa-lock"></i> ${t('Dostępny tylko dla wybranych użytkowników')}</div>` : ''}
                    </div>
                    <div class="modal-footer">
                        <button class="btn btn-primary" id="share-link-close">Zamknij</button>
                    </div>
                </div>
            `;
            document.body.appendChild(overlay2);

            const urlInput = overlay2.querySelector('#share-link-url');
            urlInput.select();

            overlay2.querySelector('#share-link-copy').addEventListener('click', () => {
                navigator.clipboard.writeText(shareUrl).then(() => {
                    toast('Link skopiowany do schowka', 'success');
                }).catch(() => {
                    urlInput.select();
                    document.execCommand('copy');
                    toast('Link skopiowany', 'success');
                });
            });
            overlay2.querySelector('#share-link-close').addEventListener('click', () => overlay2.remove());
            overlay2.addEventListener('click', (e) => { if (e.target === overlay2) overlay2.remove(); });
        } catch (e) {
            toast(t('Błąd tworzenia linku: ') + (e.message || 'nieznany'), 'error');
        }
    }

    async function shareSamba() {
        const name = [...state.selected][0];
        if (!name) return;
        const item = state.items.find(i => i.name === name);
        if (!item || !item.is_dir) { toast(t('Wybierz folder do udostępnienia'), 'warning'); return; }

        // Check if Samba is installed
        try {
            const sambaStatus = await api('/storage/samba/status');
            if (!sambaStatus.installed) {
                if (confirm(t('Samba nie jest zainstalowana.\nCzy chcesz przejść do instalacji?'))) {
                    if (typeof openApp === 'function') openApp('sharing');
                    else toast(t('Przejdź do aplikacji Dyski → Samba aby zainstalować'), 'info');
                }
                return;
            }
        } catch (e) { /* continue anyway */ }

        const fullPath = itemFullPath(name);

        // Custom modal that captures values before DOM removal
        const result = await new Promise((resolve) => {
            const overlay = document.createElement('div');
            overlay.className = 'modal-overlay';
            overlay.innerHTML = `
                <div class="modal">
                    <div class="modal-header">${t('Udostępnij folder (Samba)')}</div>
                    <div class="modal-body">
                        <div class="app-mb-md">
                            <span class="app-label-muted">${t('Ścieżka:')}</span><br>
                            <code class="app-path">${fullPath}</code>
                        </div>
                        <label class="modal-label">${t('Nazwa udziału sieciowego:')}</label>
                        <input class="modal-input" id="samba-share-name" value="${name}">
                        <div class="app-mt-md">
                            <label class="app-check-label app-check-label--secondary">
                                <input type="checkbox" id="samba-guest-ok" checked class="app-checkbox">
                                ${t('Dostęp jako gość (bez hasła)')}
                            </label>
                        </div>
                    </div>
                    <div class="modal-footer">
                        <button class="btn" id="samba-dlg-cancel">Anuluj</button>
                        <button class="btn btn-primary" id="samba-dlg-ok">${t('Udostępnij')}</button>
                    </div>
                </div>
            `;
            document.body.appendChild(overlay);
            overlay.querySelector('#samba-share-name').focus();

            overlay.querySelector('#samba-dlg-cancel').addEventListener('click', () => {
                overlay.remove();
                resolve(null);
            });
            overlay.querySelector('#samba-dlg-ok').addEventListener('click', () => {
                const shareName = overlay.querySelector('#samba-share-name').value.trim();
                const guestOk = overlay.querySelector('#samba-guest-ok').checked;
                overlay.remove();
                resolve(shareName ? { shareName, guestOk } : null);
            });
            overlay.addEventListener('click', (e) => {
                if (e.target === overlay) { overlay.remove(); resolve(null); }
            });
        });

        if (!result) return;

        try {
            const resp = await api('/storage/samba/share', {
                method: 'POST',
                body: { name: result.shareName, path: fullPath, guest_ok: result.guestOk }
            });
            if (resp.error) {
                toast(t('Błąd: ') + resp.error, 'error');
            } else {
                toast(`${t('Folder')} "${name}" ${t('udostępniony jako')} "${result.shareName}"`, 'success');
                await loadSambaShares();
                renderFileList();
            }
        } catch (e) {
            toast(t('Błąd udostępniania: ') + (e.message || 'nieznany'), 'error');
        }
    }

    async function unshareSamba() {
        const sel = [...state.selected];
        if (sel.length !== 1) return;
        const name = sel[0];
        const item = state.items.find(i => i.name === name);
        if (!item) return;
        const share = getShareForItem(item);
        if (!share) { toast(t('Ten folder nie jest udostępniony'), 'error'); return; }

        const ok = await confirmDialog(t('Cofnij udostępnianie'),
            `${t('Czy na pewno chcesz cofnąć udostępnianie')} "${share.name}"?`);
        if (!ok) return;

        try {
            const resp = await api('/storage/samba/share', {
                method: 'DELETE',
                body: { name: share.name }
            });
            if (resp.error) {
                toast(t('Błąd: ') + resp.error, 'error');
            } else {
                toast(`${t('Udostępnianie')} "${share.name}" ${t('cofnięte')}`, 'success');
                await loadSambaShares();
                renderFileList();
            }
        } catch (e) {
            toast(t('Błąd: ') + (e.message || 'nieznany'), 'error');
        }
    }

    function selectAll() {
        state.items.forEach(i => state.selected.add(i.name));
        updateSelection();
    }

    function clearSelection() {
        state.selected.clear();
        state.lastClickedIndex = -1;
        updateSelection();
    }

    // ─── Event Bindings ───
    body.querySelector('#fm-back').addEventListener('click', () => {
        if (state.historyIndex > 0) {
            state.historyIndex--;
            navigateTo(state.history[state.historyIndex]);
        }
    });
    body.querySelector('#fm-forward').addEventListener('click', () => {
        if (state.historyIndex < state.history.length - 1) {
            state.historyIndex++;
            navigateTo(state.history[state.historyIndex]);
        }
    });
    body.querySelector('#fm-up').addEventListener('click', () => {
        if (isRegularPath() && !isAtHomeRoot()) {
            const parent = state.path.split('/').slice(0, -1).join('/') || (state.sudoMode ? '/' : state.homePath);
            navigateTo(parent);
        }
    });
    body.querySelector('#fm-logs').addEventListener('click', () => showLogsDialog(state.path));
    body.querySelector('#fm-refresh').addEventListener('click', () => navigateTo(state.path));
    body.querySelector('#fm-newfolder').addEventListener('click', createNewFolder);
    body.querySelector('#fm-upload').addEventListener('click', uploadFiles);
    body.querySelector('#fm-upload-folder').addEventListener('click', uploadFolder);
    body.querySelector('#fm-download').addEventListener('click', downloadSelected);
    body.querySelector('#fm-delete').addEventListener('click', deleteSelected);

    // View mode buttons
    function updateViewModeButtons() {
        body.querySelectorAll('.fm-view-btn').forEach(btn => {
            btn.classList.toggle('active', btn.dataset.view === state.viewMode);
        });
    }
    body.querySelectorAll('.fm-view-btn').forEach(btn => {
        btn.addEventListener('click', () => setViewMode(btn.dataset.view));
    });
    updateViewModeButtons();

    // Mobile sidebar toggle
    body.querySelector('#fm-sidebar-toggle')?.addEventListener('click', () => {
        const sidebar = body.querySelector('#fm-sidebar');
        const btn = body.querySelector('#fm-sidebar-toggle');
        const isOpen = sidebar.classList.toggle('fm-sidebar-open');
        btn.setAttribute('aria-expanded', isOpen ? 'true' : 'false');
    });
    // Close sidebar when clicking outside on mobile
    body.querySelector('#fm-file-list')?.addEventListener('click', () => {
        const sidebar = body.querySelector('#fm-sidebar');
        if (sidebar.classList.contains('fm-sidebar-open') && window.innerWidth <= 600) {
            sidebar.classList.remove('fm-sidebar-open');
            body.querySelector('#fm-sidebar-toggle')?.setAttribute('aria-expanded', 'false');
        }
    });

    // Sort dropdown logic
    const sortLabels = { name: t('Nazwa'), size: t('Rozmiar'), modified: t('Data mod.'), permissions: t('Prawa') };
    function updateSortLabel() {
        const label = body.querySelector('#fm-sort-label');
        if (label) label.textContent = sortLabels[state.sortCol] || 'Nazwa';
        // Highlight active sort option
        body.querySelectorAll('#fm-sort-menu .fm-sort-option[data-sort]').forEach(opt => {
            opt.classList.toggle('active', opt.dataset.sort === state.sortCol);
        });
        body.querySelectorAll('#fm-sort-menu .fm-sort-option[data-dir]').forEach(opt => {
            opt.classList.toggle('active', (opt.dataset.dir === 'asc') === state.sortAsc);
        });
        // Update icon on button
        const icon = body.querySelector('#fm-sort-btn > i:first-child');
        if (icon) icon.className = state.sortAsc ? 'fas fa-sort-amount-up-alt' : 'fas fa-sort-amount-down-alt';
        // Update aria-sort on column headers
        body.querySelectorAll('#fm-header-columns span[data-sort]').forEach(span => {
            if (span.dataset.sort === state.sortCol) {
                span.setAttribute('aria-sort', state.sortAsc ? 'ascending' : 'descending');
            } else {
                span.setAttribute('aria-sort', 'none');
            }
        });
    }
    body.querySelector('#fm-sort-btn').addEventListener('click', (e) => {
        e.stopPropagation();
        const menu = body.querySelector('#fm-sort-menu');
        const sortBtn = body.querySelector('#fm-sort-btn');
        const isHidden = menu.classList.toggle('hidden');
        sortBtn.setAttribute('aria-expanded', !isHidden ? 'true' : 'false');
        // Close on outside click
        const closeMenu = (ev) => {
            if (!menu.contains(ev.target)) {
                menu.classList.add('hidden');
                sortBtn.setAttribute('aria-expanded', 'false');
                document.removeEventListener('click', closeMenu);
            }
        };
        if (!menu.classList.contains('hidden')) {
            setTimeout(() => document.addEventListener('click', closeMenu), 0);
        }
    });
    body.querySelector('#fm-sort-menu').addEventListener('click', (e) => {
        const opt = e.target.closest('.fm-sort-option');
        if (!opt) return;
        if (opt.dataset.sort) {
            state.sortCol = opt.dataset.sort;
        } else if (opt.dataset.dir) {
            state.sortAsc = opt.dataset.dir === 'asc';
        }
        updateSortLabel();
        renderFileList();
        body.querySelector('#fm-sort-menu').classList.add('hidden');
        body.querySelector('#fm-sort-btn').setAttribute('aria-expanded', 'false');
    });
    updateSortLabel();

    // Selection action bindings (inside header)
    body.querySelector('#fm-sel-copy').addEventListener('click', clipboardCopy);
    body.querySelector('#fm-sel-cut').addEventListener('click', clipboardCut);
    body.querySelector('#fm-sel-download').addEventListener('click', downloadSelected);
    body.querySelector('#fm-sel-delete').addEventListener('click', deleteSelected);
    body.querySelector('#fm-sel-clear').addEventListener('click', clearSelection);
    body.querySelector('#fm-clipboard-paste').addEventListener('click', clipboardPaste);
    body.querySelector('#fm-clipboard-cancel').addEventListener('click', () => {
        state.clipboard = null;
        updateClipboardBar();
    });

    // Header checkbox — select/deselect all
    body.querySelector('#fm-header-cb').addEventListener('change', (e) => {
        if (e.target.checked) selectAll();
        else clearSelection();
    });

    // ─── Delegated event handlers on #fm-file-list (registered once) ───
    const _fmList = body.querySelector('#fm-file-list');
    const _itemSel = '.fm-file-item, .fm-grid-item, .fm-thumb-item';



    _fmList.addEventListener('keydown', (e) => {
        // Keyboard navigation
        const _allItems = state.searchResults !== null ? state.searchResults : state.items;
        const total = _allItems.length;
        if (total === 0) return;

        let processed = true;

        const getCols = () => {
            if (state.viewMode === 'list') return 1;
            const items = _fmList.querySelectorAll('.fm-grid-item, .fm-thumb-item');
            if (items.length < 2) return 1;

            // Robust way: find first item on the next row
            const firstTop = items[0].getBoundingClientRect().top;
            for (let i = 1; i < items.length; i++) {
                if (items[i].getBoundingClientRect().top > firstTop + 10) {
                    return i;
                }
            }
            return items.length; // All items on one row
        };

        const handleMove = (newIdx) => {
            if (newIdx < 0) newIdx = 0;
            if (newIdx >= total) newIdx = total - 1;

            if (e.shiftKey) {
                // Range selection
                if (state.lastClickedIndex === -1) state.lastClickedIndex = state.focusedIndex;
                const start = Math.min(state.lastClickedIndex, newIdx);
                const end = Math.max(state.lastClickedIndex, newIdx);

                // If not holding Ctrl, Shift+Arrow usually clears previous discontinuous selections
                if (!e.ctrlKey && !e.metaKey) state.selected.clear();

                const sorted = sortItems(_allItems);
                for (let i = start; i <= end; i++) {
                    if (sorted[i]) state.selected.add(sorted[i].name);
                }
                updateSelection();
            } else {
                // Move anchor if not selecting
                state.lastClickedIndex = newIdx;

                // Standard desktop behavior: moving focus selects item (unless Ctrl is held)
                if (!e.ctrlKey && !e.metaKey) {
                    state.selected.clear();
                    const sorted = sortItems(_allItems);
                    if (sorted[newIdx]) state.selected.add(sorted[newIdx].name);
                    updateSelection();
                }
            }
            state.focusedIndex = newIdx;
        };

        switch(e.key) {
            case 'ArrowDown':
                handleMove(state.focusedIndex + getCols());
                break;
            case 'ArrowUp':
                handleMove(state.focusedIndex - getCols());
                break;
            case 'ArrowRight':
                handleMove(state.focusedIndex + 1);
                break;
            case 'ArrowLeft':
                handleMove(state.focusedIndex - 1);
                break;
            case 'Home':
                handleMove(0);
                break;
            case 'End':
                handleMove(total - 1);
                break;
            case 'Backspace':
                e.stopPropagation();
                e.preventDefault();
                if (state.selected.size === 0 || document.activeElement === _fmList) {
                   // Go Up
                   const upBtn = body.querySelector('#fm-up');
                   if (upBtn && !upBtn.disabled) upBtn.click();
                }
                break;
            case 'Enter':
                e.stopPropagation();
                e.preventDefault();
                if (state.focusedIndex >= 0) {
                    const sorted = sortItems(_allItems);
                    const item = sorted[state.focusedIndex];
                    if (item) {
                        if (item.is_dir) navigateTo(itemFullPath(item));
                        else previewFile(item.name);
                    }
                }
                break;
            case ' ': // Space to toggle selection
                if (state.focusedIndex >= 0) {
                    e.stopPropagation();
                    e.preventDefault(); // prevent scroll
                    const sorted = sortItems(_allItems);
                    const item = sorted[state.focusedIndex];
                    if (item) {
                        if (state.selected.has(item.name)) state.selected.delete(item.name);
                        else state.selected.add(item.name);
                        state.lastClickedIndex = state.focusedIndex;
                        updateSelection();
                    }
                }
                break;
            case 'a':
                if (e.ctrlKey || e.metaKey) {
                    e.stopPropagation();
                    e.preventDefault();
                    state.selected = new Set(_allItems.map(i => i.name));
                    updateSelection();
                } else processed = false;
                break;
            case 'Delete':
                e.stopPropagation();
                e.preventDefault();
                if (state.selected.size > 0) {
                    deleteSelected();
                } else if (state.focusedIndex >= 0) {
                     // Delete focused item if nothing selected
                     const sorted = sortItems(_allItems);
                     const item = sorted[state.focusedIndex];
                     if (item) {
                         state.selected.add(item.name);
                         updateSelection();
                         deleteSelected();
                     }
                }
                break;
            default:
                processed = false;
        }

        if (processed) {
            if (e.key.startsWith('Arrow') || e.key === 'Home' || e.key === 'End') {
                e.stopPropagation();
                e.preventDefault();
                // Ensure page change if focused item is not on current page
                const newPage = Math.floor(state.focusedIndex / state.pageSize);
                if (newPage !== state.page) {
                    state.page = newPage;
                    renderFileList();
                }
                setFocusedIndex(state.focusedIndex);
            }
        }
    });

    _fmList.addEventListener('click', (e) => {
        // Checkbox click
        const cbLabel = e.target.closest('.fm-checkbox-label');
        if (cbLabel) {
            e.preventDefault();
            e.stopPropagation();
            const name = cbLabel.dataset.cbName;
            if (state.selected.has(name)) state.selected.delete(name);
            else state.selected.add(name);
            const el = cbLabel.closest(_itemSel);
            if (el) {
                state.lastClickedIndex = parseInt(el.dataset.idx);
                state.focusedIndex = state.lastClickedIndex;
            }
            updateSelection();
            return;
        }

        const el = e.target.closest(_itemSel);
        if (!el) {
            // Click on empty space — deselect all
            state.selected.clear();
            state.lastClickedIndex = -1;
            state.focusedIndex = -1;
            updateSelection();
            return;
        }

        // Prevent browser text-selection on shift-click
        if (e.shiftKey) e.preventDefault();

        // Item click — single/ctrl/shift select
        const name = el.dataset.name;
        const idx = parseInt(el.dataset.idx);

        // Mobile/Touch: if in selection mode, behave like Ctrl-click
        const isMultiSelect = e.ctrlKey || e.metaKey || state.selectMode;

        if (e.shiftKey && state.lastClickedIndex >= 0) {
            const _allItems = state.searchResults !== null ? state.searchResults : state.items;
            const sorted = sortItems(_allItems);
            const start = Math.min(state.lastClickedIndex, idx);
            const end = Math.max(state.lastClickedIndex, idx);
            if (!isMultiSelect) state.selected.clear();
            for (let i = start; i <= end; i++) {
                if (sorted[i]) state.selected.add(sorted[i].name);
            }
        } else if (isMultiSelect) {
            if (state.selected.has(name)) state.selected.delete(name);
            else state.selected.add(name);
        } else {
            state.selected.clear();
            state.selected.add(name);
        }
        state.lastClickedIndex = idx;
        state.focusedIndex = idx;
        // Update focused visual indicator without scroll (user clicked, already visible)
        const list = body.querySelector('#fm-file-list');
        list.querySelectorAll('.fm-focused').forEach(el => el.classList.remove('fm-focused'));
        el.classList.add('fm-focused');
        list.setAttribute('aria-activedescendant', `fm-item-${idx}`);
        updateSelection();
    });

    _fmList.addEventListener('dblclick', (e) => {
        // Skip if clicking a size cell (folder size calc)
        if (e.target.closest('.fm-file-size')) return;
        const el = e.target.closest(_itemSel);
        if (!el) return;
        const name = el.dataset.name;
        // If in search mode, use the full path from search results
        if (state.searchResults !== null && el.dataset.path) {
            if (el.dataset.isdir === 'true') {
                navigateTo(el.dataset.path);
            } else {
                // Navigate to parent folder, then preview
                const parent = el.dataset.path.substring(0, el.dataset.path.lastIndexOf('/')) || '/';
                navigateTo(parent);
            }
            return;
        }
        if (el.dataset.isdir === 'true') {
            navigateTo(itemFullPath(name));
        } else {
            previewFile(name);
        }
    });

    _fmList.addEventListener('contextmenu', (e) => {
        e.preventDefault();
        const el = e.target.closest(_itemSel);
        if (el) {
            const name = el.dataset.name;
            if (!state.selected.has(name)) {
                state.selected.clear();
                state.selected.add(name);
                state.lastClickedIndex = parseInt(el.dataset.idx);
                updateSelection();
            }
        } else {
            // Clicked on empty space — clear selection
            state.selected.clear();
            updateSelection();
        }
        showFMContextMenu(e.clientX, e.clientY);
    });

    // Sort headers
    body.querySelector('#fm-list-header').addEventListener('click', (e) => {
        const span = e.target.closest('[data-sort]');
        if (!span) return;
        const col = span.dataset.sort;
        if (state.sortCol === col) state.sortAsc = !state.sortAsc;
        else { state.sortCol = col; state.sortAsc = true; }
        renderFileList();
    });

    // Drag & drop upload
    const main = body.querySelector('.fm-main');
    let dropOverlay = null;

    main.addEventListener('dragover', (e) => {
        e.preventDefault();
        if (!dropOverlay) {
            dropOverlay = document.createElement('div');
            dropOverlay.className = 'fm-dropzone';
            dropOverlay.innerHTML = `<i class="fas fa-cloud-upload-alt"></i>&nbsp; ${t('Upuść pliki tutaj')}`;
            main.appendChild(dropOverlay);
        }
    });
    main.addEventListener('dragleave', (e) => {
        if (!main.contains(e.relatedTarget)) {
            dropOverlay?.remove();
            dropOverlay = null;
        }
    });
    main.addEventListener('drop', async (e) => {
        e.preventDefault();
        dropOverlay?.remove();
        dropOverlay = null;

        // Check if any dropped items are directories (use DataTransferItem API)
        const items = e.dataTransfer.items;
        let hasDir = false;
        if (items) {
            for (let i = 0; i < items.length; i++) {
                const entry = items[i].webkitGetAsEntry?.();
                if (entry?.isDirectory) { hasDir = true; break; }
            }
        }

        if (hasDir && items) {
            // Collect all files recursively from dropped folders
            const allFiles = [];
            const readEntry = (entry, pathPrefix) => {
                return new Promise((resolve) => {
                    if (entry.isFile) {
                        entry.file(f => {
                            Object.defineProperty(f, '_relPath', { value: pathPrefix + f.name });
                            allFiles.push(f);
                            resolve();
                        }, () => resolve());
                    } else if (entry.isDirectory) {
                        const reader = entry.createReader();
                        const readAll = (entries = []) => {
                            reader.readEntries(batch => {
                                if (!batch.length) {
                                    Promise.all(entries.map(e => readEntry(e, pathPrefix + entry.name + '/'))).then(resolve);
                                } else {
                                    readAll([...entries, ...batch]);
                                }
                            }, () => resolve());
                        };
                        readAll();
                    } else {
                        resolve();
                    }
                });
            };
            const promises = [];
            for (let i = 0; i < items.length; i++) {
                const entry = items[i].webkitGetAsEntry?.();
                if (entry) promises.push(readEntry(entry, ''));
            }
            await Promise.all(promises);
            if (allFiles.length) {
                // Attach relative paths as webkitRelativePath-compatible property
                // and delegate to _doUploadWithPaths which handles chunked upload for large files
                allFiles.forEach(f => {
                    if (!f.webkitRelativePath && f._relPath) {
                        Object.defineProperty(f, 'webkitRelativePath', { value: f._relPath, configurable: true });
                    }
                });
                _doUploadWithPaths(allFiles);
            }
        } else {
            const files = e.dataTransfer.files;
            if (!files.length) return;
            _doUpload(files);
        }
    });

    // Keyboard shortcuts
    body.closest('.window').addEventListener('keydown', (e) => {
        // Ctrl+F — focus search
        if ((e.ctrlKey || e.metaKey) && (e.key === 'f' || e.key === 'F')) {
            e.preventDefault();
            body.querySelector('#fm-search-input').focus();
            return;
        }
        // Escape — clear search or selection
        if (e.key === 'Escape') {
            if (document.activeElement === body.querySelector('#fm-search-input')) {
                body.querySelector('#fm-search-clear').click();
                document.activeElement.blur();
            } else {
                clearSelection();
            }
            return;
        }

        if (['INPUT', 'TEXTAREA'].includes(e.target.tagName)) return;

        // Ctrl+A — Select All
        if ((e.ctrlKey || e.metaKey) && (e.key === 'a' || e.key === 'A')) {
            e.preventDefault();
            const allItems = state.searchResults !== null ? state.searchResults : state.items;
            state.selected.clear();
            allItems.forEach(i => state.selected.add(i.name));
            updateSelection();
            return;
        }

        // Delete
        if (e.key === 'Delete') {
            if (state.selected.size > 0) deleteSelected();
            return;
        }

        // Enter — Open
        if (e.key === 'Enter') {
             e.preventDefault();
             if (state.selected.size === 1) {
                 const name = [...state.selected][0];
                 const item = state.items.find(i => i.name === name);
                 if (item) {
                     if (item.is_dir) navigateTo(itemFullPath(item));
                     else previewFile(item.name);
                 }
             } else if (state.focusedIndex >= 0) {
                 const allItems = state.searchResults !== null ? state.searchResults : state.items;
                 const sorted = sortItems(allItems);
                 const item = sorted[state.focusedIndex];
                 if (item) {
                     if (item.is_dir) navigateTo(itemFullPath(item));
                     else previewFile(item.name);
                 }
             }
             return;
        }

        // Space — Toggle Selection
        if (e.key === ' ' || e.key === 'Spacebar') {
            e.preventDefault();
            if (state.focusedIndex >= 0) {
                const allItems = state.searchResults !== null ? state.searchResults : state.items;
                const sorted = sortItems(allItems);
                const item = sorted[state.focusedIndex];
                if (item) {
                    if (state.selected.has(item.name)) state.selected.delete(item.name);
                    else state.selected.add(item.name);
                    state.lastClickedIndex = state.focusedIndex;
                    updateSelection();
                    // Ensure focus remains
                    setFocusedIndex(state.focusedIndex);
                }
            }
            return;
        }

        // Backspace — Go Up
        if (e.key === 'Backspace') {
             e.preventDefault();
             if (isRegularPath() && !isAtHomeRoot()) {
                 const parent = state.path.split('/').slice(0, -1).join('/') || (state.sudoMode ? '/' : state.homePath);
                 navigateTo(parent);
             }
             return;
        }

        // F2 — Rename
        if (e.key === 'F2') {
            e.preventDefault();
            if (state.selected.size === 1) {
                renameSelected();
            }
            return;
        }

        // F5 or Ctrl+R — Refresh
        if (e.key === 'F5' || ((e.ctrlKey || e.metaKey) && (e.key === 'r' || e.key === 'R'))) {
            e.preventDefault();
            navigateTo(state.path);
            return;
        }

        // Ctrl+N — New Folder
        if ((e.ctrlKey || e.metaKey) && (e.key === 'n' || e.key === 'N')) {
            e.preventDefault();
            createNewFolder();
            return;
        }

        // Ctrl+U — Upload
        if ((e.ctrlKey || e.metaKey) && (e.key === 'u' || e.key === 'U')) {
            e.preventDefault();
            uploadFiles();
            return;
        }

        // Clipboard
        if ((e.ctrlKey || e.metaKey) && (e.key === 'c' || e.key === 'C')) {
            e.preventDefault();
            clipboardCopy();
            return;
        }
        if ((e.ctrlKey || e.metaKey) && (e.key === 'x' || e.key === 'X')) {
            e.preventDefault();
            clipboardCut();
            return;
        }
        if ((e.ctrlKey || e.metaKey) && (e.key === 'v' || e.key === 'V')) {
            e.preventDefault();
            clipboardPaste();
            return;
        }

        // Arrow Navigation & Paging
        if (['ArrowUp', 'ArrowDown', 'ArrowLeft', 'ArrowRight', 'PageUp', 'PageDown'].includes(e.key)) {
            e.preventDefault();
            body.querySelector('#fm-file-list').focus({ preventScroll: true });

            const allItems = state.searchResults !== null ? state.searchResults : state.items;
            const sorted = sortItems(allItems);
            if (!sorted.length) return;

            let newIdx = state.focusedIndex;
            if (newIdx < 0) newIdx = 0;

            if (state.viewMode === 'list') {
                 if (e.key === 'ArrowUp') newIdx--;
                 else if (e.key === 'ArrowDown') newIdx++;
                 else if (e.key === 'PageUp') newIdx -= 10;
                 else if (e.key === 'PageDown') newIdx += 10;
                 else if (e.key === 'ArrowLeft') {
                     // Go to parent
                     if (isRegularPath() && !isAtHomeRoot()) {
                         const parent = state.path.split('/').slice(0, -1).join('/') || (state.sudoMode ? '/' : state.homePath);
                         navigateTo(parent);
                         return;
                     }
                 }
                 else if (e.key === 'ArrowRight') {
                      // Enter folder
                      const item = sorted[newIdx];
                      if (item && item.is_dir) {
                          navigateTo(itemFullPath(item));
                          return;
                      }
                 }
            } else {
                 // Grid/Thumb view
                 const list = body.querySelector('#fm-file-list');
                 const items = Array.from(list.querySelectorAll('.fm-grid-item, .fm-thumb-item'));
                 if (!items.length) return;

                 // Robust column calculation by checking y-offset
                 const firstTop = items[0].getBoundingClientRect().top;
                 let cols = 0;
                 for (const it of items) {
                     if (Math.abs(it.getBoundingClientRect().top - firstTop) < 5) cols++;
                     else break;
                 }
                 cols = Math.max(1, cols);

                 if (e.key === 'ArrowLeft') newIdx--;
                 else if (e.key === 'ArrowRight') newIdx++;
                 else if (e.key === 'ArrowUp') newIdx -= cols;
                 else if (e.key === 'ArrowDown') newIdx += cols;
                 else if (e.key === 'PageUp') newIdx -= (cols * 5);
                 else if (e.key === 'PageDown') newIdx += (cols * 5);
            }

            // Clamp
            if (newIdx < 0) newIdx = 0;
            if (newIdx >= sorted.length) newIdx = sorted.length - 1;

            // Update selection
            if (e.shiftKey) {
                const anchor = state.lastClickedIndex >= 0 ? state.lastClickedIndex : (state.focusedIndex >= 0 ? state.focusedIndex : 0);
                const start = Math.min(anchor, newIdx);
                const end = Math.max(anchor, newIdx);
                state.selected.clear();
                for (let i = start; i <= end; i++) {
                    if (sorted[i]) state.selected.add(sorted[i].name);
                }
            } else if (!e.ctrlKey && !e.metaKey) {
                state.selected.clear();
                if (sorted[newIdx]) {
                    state.selected.add(sorted[newIdx].name);
                    state.lastClickedIndex = newIdx;
                }
            }

            setFocusedIndex(newIdx);
            updateSelection();

            // Scroll into view
            const targetEl = body.querySelector(`#fm-item-${newIdx}`);
            if (targetEl) targetEl.scrollIntoView({ block: 'nearest' });
            return;
        }

        // Home/End
        if (e.key === 'Home') {
            e.preventDefault();
            const allItems = state.searchResults !== null ? state.searchResults : state.items;
            const sorted = sortItems(allItems);
            if (!sorted.length) return;
            // Similar logic...
            const newIdx = 0;
            if (e.shiftKey) {
                const anchor = state.lastClickedIndex >= 0 ? state.lastClickedIndex : 0;
                state.selected.clear();
                for (let i = 0; i <= anchor; i++) {
                     if (sorted[i]) state.selected.add(sorted[i].name);
                }
            } else if (!e.ctrlKey) {
                state.selected.clear();
                state.selected.add(sorted[0].name);
                state.lastClickedIndex = 0;
            }
            setFocusedIndex(newIdx);
            updateSelection();
            body.querySelector(`#fm-item-${newIdx}`)?.scrollIntoView({ block: 'nearest' });
            return;
        }

        if (e.key === 'End') {
            e.preventDefault();
            const allItems = state.searchResults !== null ? state.searchResults : state.items;
            const sorted = sortItems(allItems);
            if (!sorted.length) return;
            const newIdx = sorted.length - 1;
             if (e.shiftKey) {
                const anchor = state.lastClickedIndex >= 0 ? state.lastClickedIndex : 0;
                state.selected.clear();
                for (let i = anchor; i <= newIdx; i++) {
                     if (sorted[i]) state.selected.add(sorted[i].name);
                }
            } else if (!e.ctrlKey) {
                state.selected.clear();
                state.selected.add(sorted[newIdx].name);
                state.lastClickedIndex = newIdx;
            }
            setFocusedIndex(newIdx);
            updateSelection();
            body.querySelector(`#fm-item-${newIdx}`)?.scrollIntoView({ block: 'nearest' });
            return;
        }

        if (e.ctrlKey && e.shiftKey && e.key === 'S') { e.preventDefault(); calcDirSizes(); }
        if (e.key === 'F1') { e.preventDefault(); showFMShortcutsHelp(); }
    });

    // ─── Keyboard Shortcuts Help ───
    function showFMShortcutsHelp() {
        const existing = body.querySelector('.fm-shortcuts-overlay');
        if (existing) { existing.remove(); return; }
        const overlay = document.createElement('div');
        overlay.className = 'fm-shortcuts-overlay';
        overlay.setAttribute('role', 'dialog');
        overlay.setAttribute('aria-modal', 'true');
        overlay.setAttribute('aria-label', t('Skróty klawiszowe'));
        overlay.innerHTML = `
            <div class="fm-shortcuts-panel">
                <div class="fm-shortcuts-header">
                    <span><i class="fas fa-keyboard"></i> ${t('Skróty klawiszowe')}</span>
                    <button class="fm-shortcuts-close" aria-label="Zamknij"><i class="fas fa-times"></i></button>
                </div>
                <div class="fm-shortcuts-body">
                    <div class="fm-shortcuts-col">
                        <div class="fm-shortcuts-section">Nawigacja</div>
                        <div class="fm-shortcut-row"><kbd>↑</kbd><kbd>↓</kbd> <span>${t('Poruszaj się po liście')}</span></div>
                        <div class="fm-shortcut-row"><kbd>←</kbd> <span>${t('Folder nadrzędny')}</span></div>
                        <div class="fm-shortcut-row"><kbd>→</kbd> <span>${t('Otwórz folder')}</span></div>
                        <div class="fm-shortcut-row"><kbd>Enter</kbd> <span>${t('Otwórz plik/folder')}</span></div>
                        <div class="fm-shortcut-row"><kbd>Backspace</kbd> <span>${t('Folder nadrzędny')}</span></div>
                        <div class="fm-shortcut-row"><kbd>Home</kbd><kbd>End</kbd> <span>Pierwszy/ostatni</span></div>
                        <div class="fm-shortcut-row"><kbd>PageUp</kbd><kbd>PageDown</kbd> <span>${t('Strona wyników')}</span></div>
                        <div class="fm-shortcut-row"><kbd>F5</kbd> <span>${t('Odśwież')}</span></div>
                    </div>
                    <div class="fm-shortcuts-col">
                        <div class="fm-shortcuts-section">Zaznaczanie</div>
                        <div class="fm-shortcut-row"><kbd>Space</kbd> <span>Zaznacz/odznacz</span></div>
                        <div class="fm-shortcut-row"><kbd>Ctrl+A</kbd> <span>Zaznacz wszystko</span></div>
                        <div class="fm-shortcut-row"><kbd>Shift+↑↓</kbd> <span>Zaznacz zakres</span></div>
                        <div class="fm-shortcut-row"><kbd>Shift+Home/End</kbd> <span>${t('Zaznacz do końca')}</span></div>
                        <div class="fm-shortcut-row"><kbd>Escape</kbd> <span>${t('Deselect All')}</span></div>
                        <div class="fm-shortcuts-section" style="margin-top:10px">Operacje</div>
                        <div class="fm-shortcut-row"><kbd>Ctrl+C</kbd> <span>${t('Copy')}</span></div>
                        <div class="fm-shortcut-row"><kbd>Ctrl+X</kbd> <span>${t('Cut')}</span></div>
                        <div class="fm-shortcut-row"><kbd>Ctrl+V</kbd> <span>Wklej</span></div>
                        <div class="fm-shortcut-row"><kbd>Delete</kbd> <span>${t('Move to Trash')}</span></div>
                        <div class="fm-shortcut-row"><kbd>F2</kbd> <span>${t('Zmień nazwę')}</span></div>
                        <div class="fm-shortcut-row"><kbd>Ctrl+N</kbd> <span>Nowy folder</span></div>
                        <div class="fm-shortcut-row"><kbd>Ctrl+U</kbd> <span>${t('Prześlij pliki')}</span></div>
                        <div class="fm-shortcut-row"><kbd>Ctrl+F</kbd> <span>Wyszukaj</span></div>
                        <div class="fm-shortcut-row"><kbd>F1</kbd> <span>Ten ekran pomocy</span></div>
                    </div>
                </div>
            </div>
        `;
        overlay.querySelector('.fm-shortcuts-close').addEventListener('click', () => overlay.remove());
        overlay.addEventListener('click', (e) => { if (e.target === overlay) overlay.remove(); });
        overlay.addEventListener('keydown', (e) => { if (e.key === 'Escape') overlay.remove(); });
        body.appendChild(overlay);
        overlay.querySelector('.fm-shortcuts-close').focus();
    }
    body.querySelector('#fm-shortcuts-btn')?.addEventListener('click', showFMShortcutsHelp);

    // ─── Mobile Select Mode ───
    function setFMSelectMode(on) {
        state.selectMode = on;
        const list = body.querySelector('#fm-file-list');
        const btn = body.querySelector('#fm-select-mode-btn');
        list.classList.toggle('fm-select-mode', on);
        if (btn) {
            btn.classList.toggle('active', on);
            btn.setAttribute('aria-pressed', on ? 'true' : 'false');
        }
        if (!on) clearSelection();
    }
    body.querySelector('#fm-select-mode-btn')?.addEventListener('click', () => {
        setFMSelectMode(!state.selectMode);
    });

    // ─── Mobile Long-Press to Select ───
    {
        const _fmList2 = body.querySelector('#fm-file-list');
        const _itemSel2 = '.fm-file-item, .fm-grid-item, .fm-thumb-item';
        let _lpTimer = null;
        let _lpStartX = 0, _lpStartY = 0;
        let _lpMoved = false;

        _fmList2.addEventListener('touchstart', (e) => {
            if (e.touches.length !== 1) return;
            const el = e.target.closest(_itemSel2);
            if (!el) return;
            _lpStartX = e.touches[0].clientX;
            _lpStartY = e.touches[0].clientY;
            _lpMoved = false;
            _lpTimer = setTimeout(() => {
                if (_lpMoved) return;
                // Long press: enter select mode and toggle this item
                if (!state.selectMode) setFMSelectMode(true);
                const name = el.dataset.name;
                if (state.selected.has(name)) state.selected.delete(name);
                else state.selected.add(name);
                state.lastClickedIndex = parseInt(el.dataset.idx);
                state.focusedIndex = state.lastClickedIndex;
                updateSelection();
                // Haptic feedback if available
                if (navigator.vibrate) navigator.vibrate(30);
            }, 500);
        }, { passive: true });

        _fmList2.addEventListener('touchmove', (e) => {
            if (!_lpTimer) return;
            const dx = Math.abs(e.touches[0].clientX - _lpStartX);
            const dy = Math.abs(e.touches[0].clientY - _lpStartY);
            if (dx > 8 || dy > 8) {
                _lpMoved = true;
                clearTimeout(_lpTimer);
                _lpTimer = null;
            }
        }, { passive: true });

        _fmList2.addEventListener('touchend', () => {
            clearTimeout(_lpTimer);
            _lpTimer = null;
        }, { passive: true });

        _fmList2.addEventListener('touchcancel', () => {
            clearTimeout(_lpTimer);
            _lpTimer = null;
        }, { passive: true });

        // In select mode, single tap toggles selection
        _fmList2.addEventListener('click', (e) => {
            if (!state.selectMode) return;
            const el = e.target.closest(_itemSel2);
            if (!el) return;
            // Prevent default double-open behavior in select mode
            e.stopImmediatePropagation();
            const name = el.dataset.name;
            if (state.selected.has(name)) state.selected.delete(name);
            else state.selected.add(name);
            state.lastClickedIndex = parseInt(el.dataset.idx);
            state.focusedIndex = state.lastClickedIndex;
            updateSelection();
        }, true);
    }

    // ─── Mobile Swipe Navigation (back/forward) ───
    {
        const _fmMain = body.querySelector('.fm-main');
        let _swStartX = 0, _swStartY = 0, _swMoved = false, _swOnItem = false;

        _fmMain.addEventListener('touchstart', (e) => {
            if (e.touches.length !== 1) return;
            _swStartX = e.touches[0].clientX;
            _swStartY = e.touches[0].clientY;
            _swMoved = false;
            // Only activate swipe from left/right edges (within 40px of edge) or anywhere
            _swOnItem = !!e.target.closest('.fm-file-item, .fm-grid-item, .fm-thumb-item');
        }, { passive: true });

        _fmMain.addEventListener('touchmove', (e) => {
            if (e.touches.length !== 1) return;
            const dx = e.touches[0].clientX - _swStartX;
            const dy = Math.abs(e.touches[0].clientY - _swStartY);
            if (Math.abs(dx) > 10 && dy < Math.abs(dx)) _swMoved = true;
        }, { passive: true });

        _fmMain.addEventListener('touchend', (e) => {
            if (!_swMoved) return;
            const dx = e.changedTouches[0].clientX - _swStartX;
            const dy = Math.abs(e.changedTouches[0].clientY - _swStartY);
            const SWIPE_THRESHOLD = 80;
            if (Math.abs(dx) < SWIPE_THRESHOLD || dy > Math.abs(dx) * 0.8) return;
            // Swipe right = go back
            if (dx > 0 && state.historyIndex > 0) {
                state.historyIndex--;
                navigateTo(state.history[state.historyIndex]);
            }
            // Swipe left = go forward
            if (dx < 0 && state.historyIndex < state.history.length - 1) {
                state.historyIndex++;
                navigateTo(state.history[state.historyIndex]);
            }
        }, { passive: true });
    }

    // ─── Search ───
    let _searchDebounce = null;
    const searchInput = body.querySelector('#fm-search-input');
    const searchClear = body.querySelector('#fm-search-clear');

    searchInput.addEventListener('input', () => {
        clearTimeout(_searchDebounce);
        const q = searchInput.value.trim();
        searchClear.classList.toggle('hidden', !q);
        if (!q) {
            state.searchResults = null;
            state.page = 0;
            renderFileList();
            return;
        }
        _searchDebounce = setTimeout(async () => {
            try {
                searchInput.classList.add('fm-searching');
                const data = await api(`/files/search?path=${encodeURIComponent(state.path)}&q=${encodeURIComponent(q)}`);
                state.searchResults = data.items || [];
                state.page = 0;
                renderFileList();
                if (data.truncated) {
                    toast(`${t('Pokazano')} ${state.searchResults.length} ${t('wyników (więcej wyników obcięto)')}`, 'info');
                }
            } catch {
                toast(t('Błąd wyszukiwania'), 'error');
            } finally {
                searchInput.classList.remove('fm-searching');
            }
        }, 400);
    });

    searchClear.addEventListener('click', () => {
        searchInput.value = '';
        searchClear.classList.add('hidden');
        state.searchResults = null;
        state.page = 0;
        renderFileList();
        searchInput.focus();
    });

    // ─── Pagination ───
    body.querySelector('#fm-page-prev').addEventListener('click', () => {
        if (state.page > 0) { state.page--; renderFileList(); body.querySelector('#fm-file-list').scrollTop = 0; }
    });
    body.querySelector('#fm-page-next').addEventListener('click', () => {
        const allItems = state.searchResults !== null ? state.searchResults : state.items;
        const totalPages = Math.ceil(allItems.length / state.pageSize);
        if (state.page < totalPages - 1) { state.page++; renderFileList(); body.querySelector('#fm-file-list').scrollTop = 0; }
    });

    // ─── Folder sizes ───

    // Update only the size cells for known dir sizes (avoids full DOM rebuild)
    function _updateDirSizesInPlace(newSizes) {
        if (!newSizes || !Object.keys(newSizes).length) return;
        if (state.viewMode !== 'list') return;  // only list view shows size column
        const list = body.querySelector('#fm-file-list');
        if (!list) return;
        const dirItems = list.querySelectorAll('.fm-file-item[data-isdir="true"]');
        for (const el of dirItems) {
            const fullPath = itemFullPath({ name: el.dataset.name, is_dir: true });
            if (fullPath in newSizes) {
                const sizeCell = el.querySelector('.fm-file-size');
                if (sizeCell) sizeCell.textContent = formatBytes(newSizes[fullPath]);
            }
        }
    }

    // Background dir-size: start async calculation, poll until done
    async function startBgDirSizes() {
        const dirs = state.items.filter(i => i.is_dir);
        if (!dirs.length) return;
        // Skip pseudo-filesystem paths that would block the server
        const _skipPaths = new Set(['/proc', '/sys', '/dev', '/run', '/snap']);
        const paths = dirs.map(d => itemFullPath(d)).filter(p =>
            !(p in state.dirSizes) && !_skipPaths.has(p)
        );
        if (!paths.length) return;
        state._dirSizePollInterval = 1500;  // reset backoff
        try {
            const data = await api('/files/dir-sizes-start', { method: 'POST', body: { paths } });
            if (!data || data.error) return;
            // Apply immediately available cached results (partial DOM update)
            if (data.cached && Object.keys(data.cached).length) {
                Object.assign(state.dirSizes, data.cached);
                _updateDirSizesInPlace(data.cached);
            }
            // Set up polling for pending jobs
            if (data.jobs && Object.keys(data.jobs).length) {
                Object.assign(state._dirSizeJobs, data.jobs);
                if (!state._dirSizePollTimer) {
                    state._dirSizePollTimer = setTimeout(pollBgDirSizes, state._dirSizePollInterval);
                }
            }
        } catch {
            // silently ignore — background operation
        }
    }

    async function pollBgDirSizes() {
        state._dirSizePollTimer = null;
        if (!Object.keys(state._dirSizeJobs).length) return;
        try {
            const data = await api('/files/dir-sizes-result', { method: 'POST', body: { jobs: state._dirSizeJobs } });
            if (!data || data.error) return;
            if (data.sizes && Object.keys(data.sizes).length) {
                Object.assign(state.dirSizes, data.sizes);
                // Remove completed jobs
                for (const path of Object.keys(data.sizes)) {
                    delete state._dirSizeJobs[path];
                }
                // Partial DOM update instead of full rebuild
                _updateDirSizesInPlace(data.sizes);
                // Reset backoff when we get results
                state._dirSizePollInterval = 1500;
            } else {
                // No new results — apply exponential backoff (1.5s → 3s → 6s → 10s max)
                state._dirSizePollInterval = Math.min((state._dirSizePollInterval || 1500) * 2, 10000);
            }
            // Remove paths that have no pending jobs (server discarded)
            if (data.pending) {
                const pendingSet = new Set(data.pending);
                for (const path of Object.keys(state._dirSizeJobs)) {
                    if (!pendingSet.has(path)) delete state._dirSizeJobs[path];
                }
            }
            if (Object.keys(state._dirSizeJobs).length) {
                state._dirSizePollTimer = setTimeout(pollBgDirSizes, state._dirSizePollInterval);
            }
        } catch {
            // silently ignore — retry with backoff
            state._dirSizePollInterval = Math.min((state._dirSizePollInterval || 1500) * 2, 10000);
            if (Object.keys(state._dirSizeJobs).length) {
                state._dirSizePollTimer = setTimeout(pollBgDirSizes, state._dirSizePollInterval);
            }
        }
    }

    async function calcDirSizes() {
        const dirs = state.items.filter(i => i.is_dir);
        if (!dirs.length) { toast(t('Brak folderów'), 'info'); return; }
        toast(`${t('Obliczanie rozmiaru')} ${dirs.length} ${t('folder(ów)...')}`, 'info');
        const paths = dirs.map(d => itemFullPath(d));
        try {
            const data = await api('/files/dir-sizes', { method: 'POST', body: { paths } });
            if (data.sizes) {
                Object.assign(state.dirSizes, data.sizes);
                renderFileList();
            }
        } catch {
            toast(t('Błąd obliczania rozmiarów'), 'error');
        }
    }

    // double-click on dash-size "—" to calc single folder size
    body.querySelector('#fm-file-list').addEventListener('dblclick', async (e) => {
        const sizeCell = e.target.closest('.fm-file-size');
        if (!sizeCell) return;
        const row = sizeCell.closest('[data-isdir="true"]');
        if (!row) return;
        const name = row.dataset.name;
        const item = state.items.find(i => i.name === name && i.is_dir);
        if (!item) return;
        const fp = itemFullPath(item);
        if (fp in state.dirSizes) return; // already calculated
        sizeCell.textContent = '...';
        try {
            const data = await api('/files/dir-sizes', { method: 'POST', body: { paths: [fp] } });
            if (data.sizes) {
                Object.assign(state.dirSizes, data.sizes);
                sizeCell.textContent = formatBytes(state.dirSizes[fp] || 0);
            }
        } catch { sizeCell.textContent = 'err'; }
    }, true);

    // ── Hover-prefetch: pre-load listing when user hovers a folder for 300 ms ──
    body.querySelector('#fm-file-list').addEventListener('mouseover', (e) => {
        const item = e.target.closest('[data-isdir="true"]');
        if (!item || item._prefetchTimer !== undefined) return;
        item._prefetchTimer = setTimeout(() => {
            delete item._prefetchTimer;
            const name = item.dataset.name;
            const folderItem = state.items.find(i => i.name === name && i.is_dir);
            if (!folderItem) return;
            const fp = itemFullPath(folderItem);
            const cached = _fmPrefetchCache.get(fp);
            if (cached && (Date.now() - cached.ts) < _FM_PREFETCH_TTL) return;
            // Pre-fetch listing in background and store in cache
            api(`/files/list?path=${encodeURIComponent(fp)}`)
                .then(d => { if (d && !d.error) _fmPrefetchCache.set(fp, { data: d, ts: Date.now() }); })
                .catch(() => {});
            // Also pre-populate server listing cache for subdirs
            api('/files/preload-cache', { method: 'POST', body: { path: fp } }).catch(() => {});
        }, 300);
    }, true);
    body.querySelector('#fm-file-list').addEventListener('mouseout', (e) => {
        const item = e.target.closest('[data-isdir="true"]');
        if (!item || item._prefetchTimer === undefined) return;
        clearTimeout(item._prefetchTimer);
        delete item._prefetchTimer;
    }, true);

    // Ctrl+Shift+S to calculate all dir sizes
    body.closest('.window').addEventListener('keydown', (e) => {
        if (e.ctrlKey && e.shiftKey && e.key === 'S') {
            e.preventDefault();
            calcDirSizes();
        }
    });



    // ─── Disk Analytics Panel ───
    const anaPanel = body.querySelector('#fm-ana-panel');
    const anaState = { path: '/', entries: [], files: [], totalSize: 0, history: [], activeTab: 'dirs', loading: false };

    body.querySelector('#fm-analyze').onclick = () => {
        anaPanel.style.display = '';
        anaState.path = state.path;
        anaState.history = [];
        renderAnaBreadcrumbs();
    };
    body.querySelector('#fm-ana-back-btn').onclick = () => {
        if (anaState.history.length) {
            const prev = anaState.history.pop();
            runAnalysis(prev);
        } else {
            anaPanel.style.display = 'none';
        }
    };
    body.querySelector('#fm-ana-scan').onclick = () => {
        anaState.history = [];
        anaState.path = state.path;
        runAnalysis();
    };

    // Sub-tab switching
    body.querySelectorAll('.fm-ana-sub-tab[data-anatab]').forEach(tab => {
        tab.onclick = () => {
            body.querySelectorAll('.fm-ana-sub-tab[data-anatab]').forEach(t => t.classList.remove('active'));
            tab.classList.add('active');
            anaState.activeTab = tab.dataset.anatab;
            if (anaState.activeTab === 'dirs') renderAnaDirs(); else renderAnaFiles();
        };
    });

    function anaFmtSize(b) {
        if (b == null) return '-';
        const u = ['B','KB','MB','GB','TB']; let i = 0, s = b;
        while (s >= 1024 && i < u.length - 1) { s /= 1024; i++; }
        return s.toFixed(i > 0 ? 1 : 0) + ' ' + u[i];
    }

    async function runAnalysis(path) {
        if (anaState.loading) return;
        anaState.loading = true;
        anaState.path = path || state.path || '/';
        const content = body.querySelector('#fm-ana-content');
        let sec = 0;
        const timerId = setInterval(() => { sec++; const el = content.querySelector('.ana-timer'); if (el) el.textContent = sec + 's'; }, 1000);
        content.innerHTML = `<div class="app-empty"><i class="fas fa-spinner fa-spin app-spinner-lg"></i><div class="app-mt-md">${t('Analizowanie')} <b>${anaState.path}</b>... <span class="ana-timer">0s</span></div></div>`;
        try {
            const [dirData, fileData] = await Promise.all([
                api(`/storage/analyze?path=${encodeURIComponent(anaState.path)}&limit=60`),
                api(`/storage/analyze/files?path=${encodeURIComponent(anaState.path)}&limit=40`)
            ]);
            anaState.entries = dirData.entries || [];
            anaState.totalSize = dirData.total_size || 0;
            anaState.files = fileData.files || [];
            body.querySelector('#fm-ana-summary').style.display = '';
            body.querySelector('#fm-ana-tabs').style.display = '';
            renderAnaSummary(dirData);
            renderAnaBreadcrumbs();
            if (anaState.activeTab === 'dirs') renderAnaDirs(); else renderAnaFiles();
        } catch (e) {
            content.innerHTML = `<div class="app-empty app-empty--error"><i class="fas fa-exclamation-triangle app-spinner-lg"></i><div class="app-mt-md">${t('Błąd:')} ${e.message}</div></div>`;
        } finally { clearInterval(timerId); anaState.loading = false; }
    }

    function renderAnaSummary(data) {
        const el = body.querySelector('#fm-ana-summary');
        const palette = ['#3b82f6','#10b981','#f59e0b','#ef4444','#8b5cf6','#ec4899','#06b6d4','#84cc16','#f97316','#6366f1'];
        const top5 = anaState.entries.slice(0, 8);
        const topSum = top5.reduce((a, e) => a + e.size, 0);
        const other = Math.max(0, anaState.totalSize - topSum);
        el.innerHTML = `
            <div class="app-summary-row">
                <div class="app-stat-block"><div class="app-stat-hero">${data.total_size_human}</div><div class="app-sublabel">${t('Łącznie')}</div></div>
                <div class="app-flex-fill-200">
                    <div class="app-bar-track">
                        ${top5.map((e, i) => `<div title="${e.name}: ${e.size_human} (${e.percent}%)" style="width:${e.percent}%;background:${palette[i]};min-width:${e.percent > 0.5 ? '2px' : '0'}"></div>`).join('')}
                        ${other > 0 ? `<div title="Inne: ${anaFmtSize(other)}" class="app-bar-other"></div>` : ''}
                    </div>
                    <div class="app-legend">
                        ${top5.map((e, i) => `<span class="app-legend-item"><span class="app-swatch" style="background:${palette[i]}"></span>${e.name}</span>`).join('')}
                    </div>
                </div>
            </div>`;
    }

    function renderAnaBreadcrumbs() {
        const el = body.querySelector('#fm-ana-breadcrumbs');
        const parts = anaState.path.split('/').filter(Boolean);
        let crumbs = ['<a href="#" data-anapath="/" class="fm-ana-crumb">/</a>'];
        let cum = '';
        parts.forEach(p => { cum += '/' + p; crumbs.push(`<span class="app-crumb-sep">/</span><a href="#" data-anapath="${cum}" class="fm-ana-crumb">${p}</a>`); });
        el.innerHTML = crumbs.join('');
        el.querySelectorAll('a[data-anapath]').forEach(a => { a.onclick = (e) => { e.preventDefault(); anaDrill(a.dataset.anapath); }; });
    }

    function anaDrill(path) { anaState.history.push(anaState.path); runAnalysis(path); }

    function renderAnaDirs() {
        const content = body.querySelector('#fm-ana-content');
        if (!anaState.entries.length) { content.innerHTML = `<div class="app-empty app-empty--sm">${t('Brak podkatalogów')}</div>`; return; }
        content.innerHTML = `<div class="fm-ana-list">${anaState.entries.map(e => {
            const barW = Math.max(1, (e.size / anaState.totalSize) * 100);
            const color = e.percent > 50 ? '#ef4444' : e.percent > 25 ? '#f59e0b' : '#3b82f6';
            return `<div class="fm-ana-row" data-anapath="${e.path}"><div class="fm-ana-row-icon"><i class="fas fa-folder app-icon-amber"></i></div><div class="fm-ana-row-info"><div class="fm-ana-row-name">${e.name}</div><div class="fm-ana-row-bar"><div class="fm-ana-row-bar-fill" style="width:${barW}%;background:${color}"></div></div></div><div class="fm-ana-row-size">${e.size_human}</div><div class="fm-ana-row-pct">${e.percent}%</div></div>`;
        }).join('')}</div>`;
        content.querySelectorAll('.fm-ana-row[data-anapath]').forEach(row => { row.onclick = () => anaDrill(row.dataset.anapath); });
    }

    function anaFileIcon(ext) {
        const m = {'.mp4':'fa-film','.mkv':'fa-film','.avi':'fa-film','.mov':'fa-film','.mp3':'fa-music','.flac':'fa-music','.wav':'fa-music','.jpg':'fa-image','.jpeg':'fa-image','.png':'fa-image','.gif':'fa-image','.webp':'fa-image','.zip':'fa-file-archive','.tar':'fa-file-archive','.gz':'fa-file-archive','.rar':'fa-file-archive','.7z':'fa-file-archive','.pdf':'fa-file-pdf','.iso':'fa-compact-disc','.db':'fa-database','.sql':'fa-database'};
        return m[ext] || 'fa-file';
    }
    function anaFileColor(ext) {
        if (['.mp4','.mkv','.avi','.mov','.ts'].includes(ext)) return '#a78bfa';
        if (['.mp3','.flac','.wav','.aac','.ogg'].includes(ext)) return '#ec4899';
        if (['.jpg','.jpeg','.png','.gif','.webp','.svg'].includes(ext)) return '#10b981';
        if (['.zip','.tar','.gz','.rar','.7z','.xz'].includes(ext)) return '#f97316';
        return '#64748b';
    }

    function renderAnaFiles() {
        const content = body.querySelector('#fm-ana-content');
        if (!anaState.files.length) { content.innerHTML = `<div class="app-empty app-empty--sm">${t('Nie znaleziono plików')}</div>`; return; }
        const maxSize = anaState.files[0]?.size || 1;
        content.innerHTML = `<div class="fm-ana-list">${anaState.files.map(f => {
            const barW = Math.max(1, (f.size / maxSize) * 100);
            const relPath = f.path.replace(anaState.path.replace(/\/$/, ''), '').replace(/^\//, '');
            const dir = relPath.includes('/') ? relPath.substring(0, relPath.lastIndexOf('/')) : '';
            return `<div class="fm-ana-row fm-ana-file-row"><div class="fm-ana-row-icon"><i class="fas ${anaFileIcon(f.ext)}" style="color:${anaFileColor(f.ext)}"></i></div><div class="fm-ana-row-info"><div class="fm-ana-row-name" title="${f.path}">${f.name}</div><div class="app-file-subpath">${dir || '.'}</div><div class="fm-ana-row-bar"><div class="fm-ana-row-bar-fill" style="width:${barW}%;background:${anaFileColor(f.ext)}"></div></div></div><div class="fm-ana-row-size">${f.size_human}</div><div class="fm-ana-row-pct"><span class="fm-badge app-text-2xs">${f.ext || '?'}</span></div></div>`;
        }).join('')}</div>`;
    }

    // Initial load
    loadFavorites();
    loadPhotoFavorites();
    loadSambaShares();
    _fmLoadGallerySources();
    navigateTo(state.path).then(() => {
        if (state.initialSelect) {
            state.selected.clear();
            state.selected.add(state.initialSelect);
            renderFileList();
            setTimeout(() => {
                const itemEl = body.querySelector(`.fm-file-item[data-name="${CSS.escape(state.initialSelect)}"]`);
                if (itemEl) itemEl.scrollIntoView({block: 'center', behavior: 'smooth'});
            }, 100);
            state.initialSelect = null;
        }
    });

    // Expose navigateTo for external callers (notifications, etc.)
    body._fmNavigateTo = navigateTo;

    // ─── Marquee Selection (Drag Select) ───
    function initMarqueeSelection() {
        const list = body.querySelector('#fm-file-list');
        if (!list) return;

        let selectionBox = null;
        let startX, startY;
        let initialSelected;

        const onMouseMove = (e) => {
            if (!selectionBox) return;

            const currentX = e.clientX;
            const currentY = e.clientY;

            const x = Math.min(startX, currentX);
            const y = Math.min(startY, currentY);
            const w = Math.abs(currentX - startX);
            const h = Math.abs(currentY - startY);

            selectionBox.style.left = x + 'px';
            selectionBox.style.top = y + 'px';
            selectionBox.style.width = w + 'px';
            selectionBox.style.height = h + 'px';

            const boxRect = selectionBox.getBoundingClientRect();
            const items = list.querySelectorAll('.fm-file-item, .fm-grid-item, .fm-thumb-item');

            state.selected = new Set(initialSelected);
            let changed = false;

            items.forEach(item => {
                const itemRect = item.getBoundingClientRect();
                if (rectsIntersect(boxRect, itemRect)) {
                    state.selected.add(item.dataset.name);
                    changed = true;
                }
            });

            if (changed || state.selected.size !== initialSelected.size) {
                updateSelection();
            }
        };

        const onMouseUp = (e) => {
             if (selectionBox) selectionBox.remove();
             selectionBox = null;
             document.removeEventListener('mousemove', onMouseMove);
             document.removeEventListener('mouseup', onMouseUp);
        };

        list.addEventListener('mousedown', (e) => {
            if (e.button !== 0) return;
            if (e.target.closest('.fm-file-item, .fm-grid-item, .fm-thumb-item')) return;
            if (e.target.closest('.fm-checkbox-label')) return;
            // Ignore if clicking scrollbar
            if (e.target === list && e.offsetX > list.clientWidth) return;

            e.preventDefault(); // prevent text selection

            startX = e.clientX;
            startY = e.clientY;

            if (!e.ctrlKey && !e.metaKey) {
                state.selected.clear();
                updateSelection();
                initialSelected = new Set();
            } else {
                initialSelected = new Set(state.selected);
            }

            selectionBox = document.createElement('div');
            selectionBox.className = 'fm-selection-box';
            selectionBox.style.left = startX + 'px';
            selectionBox.style.top = startY + 'px';
            selectionBox.style.width = '0px';
            selectionBox.style.height = '0px';
            document.body.appendChild(selectionBox);

            document.addEventListener('mousemove', onMouseMove);
            document.addEventListener('mouseup', onMouseUp);
        });
    }

    function rectsIntersect(r1, r2) {
        return !(r2.left > r1.right ||
                 r2.right < r1.left ||
                 r2.top > r1.bottom ||
                 r2.bottom < r1.top);
    }

    // Initialize Marquee
    initMarqueeSelection();
}


// ═══════════════════════════════════════════════════════════
//  DASHBOARD
// ═══════════════════════════════════════════════════════════

AppRegistry['dashboard'] = function (appDef) {
    createWindow('dashboard', {
        title: t('Pulpit'),
        icon: appDef.icon,
        iconColor: appDef.color,
        width: 900,
        height: 620,
        onRender: (body) => renderDashboard(body),
    });
};

function renderDashboard(body) {
    body.innerHTML = `
        <div class="dash">
            <div class="dash-toolbar">
                <span class="dash-toolbar-title"><i class="fas fa-tachometer-alt"></i> Dashboard</span>
                <div style="flex:1;"></div>
                <button class="tk-act-btn" id="dash-refresh-btn" title="${t('Odśwież')}">
                    <i class="fas fa-arrows-rotate"></i>
                </button>
            </div>
            <div class="dash-grid">
                <div class="dash-card" id="dash-cpu">
                    <div class="dash-card-header">
                        <div class="dash-card-title"><i class="fas fa-microchip"></i> CPU</div>
                    </div>
                    <div class="dash-gauge" id="dash-cpu-gauge">
                        <svg viewBox="0 0 100 100" width="100" height="100">
                            <circle class="dash-gauge-circle dash-gauge-bg" cx="50" cy="50" r="42"/>
                            <circle class="dash-gauge-circle dash-gauge-fill" cx="50" cy="50" r="42"
                                stroke="#3b82f6" stroke-dasharray="263.9" stroke-dashoffset="263.9"
                                id="dash-cpu-fill"/>
                        </svg>
                        <div class="dash-gauge-text" id="dash-cpu-text">0%</div>
                    </div>
                </div>

                <div class="dash-card" id="dash-ram">
                    <div class="dash-card-header">
                        <div class="dash-card-title"><i class="fas fa-memory"></i> ${t('Pamięć RAM')}</div>
                    </div>
                    <div class="dash-gauge" id="dash-ram-gauge">
                        <svg viewBox="0 0 100 100" width="100" height="100">
                            <circle class="dash-gauge-circle dash-gauge-bg" cx="50" cy="50" r="42"/>
                            <circle class="dash-gauge-circle dash-gauge-fill" cx="50" cy="50" r="42"
                                stroke="#22c55e" stroke-dasharray="263.9" stroke-dashoffset="263.9"
                                id="dash-ram-fill"/>
                        </svg>
                        <div class="dash-gauge-text" id="dash-ram-text">0%</div>
                    </div>
                    <div class="app-detail-center" id="dash-ram-detail"></div>
                </div>

                <div class="dash-card" id="dash-net">
                    <div class="dash-card-header">
                        <div class="dash-card-title"><i class="fas fa-network-wired"></i> ${t('Sieć')}</div>
                    </div>
                    <div class="app-stats-row">
                        <div class="app-text-center">
                            <div class="app-sublabel">
                                <i class="fas fa-arrow-up app-icon-success"></i> ${t('Wysyłanie')}
                            </div>
                            <div class="app-stat-value" id="dash-net-up">0 B/s</div>
                        </div>
                        <div class="app-text-center">
                            <div class="app-sublabel">
                                <i class="fas fa-arrow-down app-icon-info"></i> Pobieranie
                            </div>
                            <div class="app-stat-value" id="dash-net-down">0 B/s</div>
                        </div>
                    </div>
                </div>

                <div class="dash-card" id="dash-uptime">
                    <div class="dash-card-header">
                        <div class="dash-card-title"><i class="fas fa-clock"></i> Informacje</div>
                    </div>
                    <div id="dash-info-rows"></div>
                </div>

                <div class="dash-card" id="dash-ups" style="display:none">
                    <div class="dash-card-header">
                        <div class="dash-card-title"><i class="fas fa-battery-full"></i> UPS</div>
                    </div>
                    <div id="dash-ups-content" class="app-text-center"></div>
                </div>

                <div class="dash-card full-width" id="dash-disks">
                    <div class="dash-card-header">
                        <div class="dash-card-title"><i class="fas fa-hdd"></i> Dyski</div>
                    </div>
                    <div id="dash-disk-list"></div>
                </div>

                <div class="dash-card full-width" id="dash-smart">
                    <div class="dash-card-header">
                        <div class="dash-card-title"><i class="fas fa-user-md"></i> S.M.A.R.T.</div>
                    </div>
                    <div id="dash-smart-list" class="ddisk-group-cards"></div>
                </div>

                <div class="dash-card full-width" id="dash-docker">
                    <div class="dash-card-header">
                        <div class="dash-card-title"><i class="fas fa-cubes"></i> Docker</div>
                        <span class="app-label-muted" id="dash-docker-count"></span>
                    </div>
                    <div id="dash-docker-list" class="app-chip-list"></div>
                </div>
            </div>
        </div>
    `;

    const circumference = 2 * Math.PI * 42; // ~263.9

    function setGauge(fillId, textId, percent, color) {
        const fill = body.querySelector('#' + fillId);
        const text = body.querySelector('#' + textId);
        if (!fill || !text) return;
        const offset = circumference - (percent / 100) * circumference;
        fill.style.strokeDashoffset = offset;
        fill.style.stroke = color;
        text.textContent = Math.round(percent) + '%';
    }

    function colorForPercent(p) {
        if (p > 80) return '#ef4444';
        if (p > 50) return '#eab308';
        return p > -1 ? '#22c55e' : '#3b82f6';
    }

    async function loadSystemInfo() {
        try {
            const info = await api('/system/info');

            // CPU
            setGauge('dash-cpu-fill', 'dash-cpu-text', info.cpu_percent, colorForPercent(info.cpu_percent));

            // RAM
            setGauge('dash-ram-fill', 'dash-ram-text', info.memory.percent, colorForPercent(info.memory.percent));
            const ramDetail = body.querySelector('#dash-ram-detail');
            if (ramDetail) ramDetail.textContent = `${formatBytes(info.memory.used)} / ${formatBytes(info.memory.total)}`;

            // Info
            const infoRows = body.querySelector('#dash-info-rows');
            if (infoRows) {
                infoRows.innerHTML = `
                    <div class="dash-info-row"><span class="dash-info-label">Hostname</span><span class="dash-info-value">${info.hostname}</span></div>
                    <div class="dash-info-row"><span class="dash-info-label">CPU rdzeni</span><span class="dash-info-value">${info.cpu_count}</span></div>
                    <div class="dash-info-row"><span class="dash-info-label">Temperatura</span><span class="dash-info-value">${info.cpu_temp ? info.cpu_temp.toFixed(1) + '°C' : '—'}</span></div>
                    <div class="dash-info-row"><span class="dash-info-label">Uptime</span><span class="dash-info-value">${formatUptime(info.uptime)}</span></div>
                `;
            }

            // UPS
            const upsCard = body.querySelector('#dash-ups');
            const upsContent = body.querySelector('#dash-ups-content');
            if (upsCard && info.ups) {
                upsCard.style.display = 'flex';
                if (info.ups.connected) {
                    const charge = info.ups.battery_charge;
                    const status = info.ups.status;
                    const runtime = info.ups.runtime;

                    let icon = 'fa-battery-full';
                    let color = 'var(--success)';
                    if (charge < 20) { icon = 'fa-battery-quarter'; color = 'var(--danger)'; }
                    else if (charge < 50) { icon = 'fa-battery-half'; color = 'var(--warning)'; }

                    if (status.includes('OB')) { color = 'var(--danger)'; icon = 'fa-plug-circle-xmark'; }

                    upsContent.innerHTML = `
                        <div style="font-size:2em;color:${color}"><i class="fas ${icon}"></i> ${charge}%</div>
                        <div class="app-sublabel">${info.ups.model}</div>
                        <div class="app-stat-value" style="font-size:0.9em;margin-top:5px">
                            ${status} ${runtime > 0 ? '· ' + formatUptime(runtime) : ''}
                        </div>
                    `;
                } else {
                    upsContent.innerHTML = `
                        <div style="font-size:2em;color:var(--text-muted)"><i class="fas fa-plug-circle-xmark"></i> --%</div>
                        <div class="app-sublabel">Brak UPS</div>
                        <div class="app-stat-value" style="font-size:0.9em;margin-top:5px">
                            ${t('Nie wykryto urządzenia')}
                        </div>
                    `;
                }
                upsCard.onclick = () => openApp('ups');
                upsCard.style.cursor = 'pointer';
            } else if (upsCard) {
                upsCard.style.display = 'none';
            }

            // Disks
            const diskList = body.querySelector('#dash-disk-list');
            if (diskList) {
                const filteredDisks = info.disks.filter(d =>
                    !d.mountpoint.startsWith('/snap') &&
                    !d.mountpoint.startsWith('/boot') &&
                    d.total > 0
                );

                // Categorize disks
                function diskCategory(d) {
                    if (d.is_usb) return 'usb';
                    if (d.mountpoint.startsWith('/media/')) return 'external';
                    return 'system';
                }
                function diskLabel(d) {
                    if (d.label) return d.label;
                    if (d.mountpoint === '/') return 'System /';
                    const parts = d.mountpoint.split('/');
                    return parts[parts.length - 1] || d.mountpoint;
                }
                function diskIcon(d) {
                    const cat = diskCategory(d);
                    if (cat === 'usb') return 'fa-usb';
                    if (cat === 'external') return 'fa-hdd';
                    if (d.mountpoint === '/') return 'fa-server';
                    return 'fa-hdd';
                }
                function diskIconColor(d) {
                    const cat = diskCategory(d);
                    if (cat === 'usb') return '#a78bfa';
                    if (cat === 'external') return '#f59e0b';
                    return '#3b82f6';
                }

                const systemDisks = filteredDisks.filter(d => diskCategory(d) === 'system');
                const usbDisks = filteredDisks.filter(d => diskCategory(d) === 'usb');
                const extDisks = filteredDisks.filter(d => diskCategory(d) === 'external');

                function renderDiskCard(d) {
                    const freeBytes = d.total - d.used;
                    const color = colorForPercent(d.percent);
                    const modelLine = d.model ? `<span class="app-model-text">${d.model}</span>` : '';
                    return `
                    <div class="ddisk-card">
                        <div class="ddisk-icon" style="color:${diskIconColor(d)}">
                            <i class="fas ${diskIcon(d)}"></i>
                        </div>
                        <div class="ddisk-info">
                            <div class="ddisk-name">${diskLabel(d)} ${modelLine}</div>
                            <div class="ddisk-meta">${d.device} · ${d.fstype || '?'} · ${d.mountpoint}</div>
                            <div class="ddisk-bar-wrap">
                                <div class="ddisk-bar">
                                    <div class="ddisk-bar-fill" style="width:${d.percent}%;background:${color}"></div>
                                </div>
                                <span class="ddisk-pct" style="color:${color}">${Math.round(d.percent)}%</span>
                            </div>
                            <div class="ddisk-sizes">
                                <span><b>${formatBytes(d.used)}</b> ${t('zajęte')}</span>
                                <span><b>${formatBytes(freeBytes)}</b> wolne</span>
                                <span>z <b>${formatBytes(d.total)}</b></span>
                            </div>
                        </div>
                    </div>`;
                }

                function renderGroup(title, icon, disks) {
                    if (!disks.length) return '';
                    return `
                        <div class="ddisk-group">
                            <div class="ddisk-group-title"><i class="fas ${icon}"></i> ${title}</div>
                            <div class="ddisk-group-cards">
                                ${disks.map(renderDiskCard).join('')}
                            </div>
                        </div>`;
                }

                diskList.innerHTML =
                    renderGroup('Dyski systemowe', 'fa-server', systemDisks) +
                    renderGroup(t('Dyski zewnętrzne (montowane)'), 'fa-hdd', extDisks) +
                    renderGroup('Dyski USB', 'fa-usb', usbDisks) +
                    (filteredDisks.length === 0 ? `<div class="app-empty app-empty--compact">${t('Brak dysków')}</div>` : '');
            }
        } catch {
            // ignore
        }
    }

    async function loadDockerSummary() {
        try {
            const containers = await api('/docker/containers');
            const running = containers.filter(c => c.state === 'running').length;
            const total = containers.length;

            body.querySelector('#dash-docker-count').textContent = `${running}/${total} uruchomionych`;

            const list = body.querySelector('#dash-docker-list');
            list.innerHTML = containers.slice(0, 20).map(c => {
                const color = c.state === 'running' ? 'var(--success)' : c.state === 'paused' ? 'var(--warning)' : 'var(--text-muted)';
                return `<div class="app-chip">
                    <span class="app-dot" style="background:${color}"></span>
                    ${c.name}
                </div>`;
            }).join('');
        } catch {
            // ignore
        }
    }

    async function loadSmartInfo() {
        try {
            const data = await api('/resources/smart');
            const smartList = body.querySelector('#dash-smart-list');
            if (!smartList) return;

            if (!data || !data.length) {
                smartList.innerHTML = `<div class="app-empty app-empty--compact">${t('Brak danych S.M.A.R.T.')}</div>`;
                return;
            }

            smartList.innerHTML = data.map(d => {
                const statusColor = d.health === 'PASS' ? 'var(--success)' : 'var(--danger)';
                const icon = d.type === 'nvme' ? 'fa-memory' : 'fa-hdd';
                const temp = d.temperature > 0 ? d.temperature + '°C' : '—';
                const tempColor = d.temperature > 60 ? 'var(--danger)' : d.temperature > 50 ? 'var(--warning)' : 'var(--text-muted)';
                let life = '';
                if (d.remaining_life !== -1) {
                    const lifeColor = d.remaining_life < 10 ? 'var(--danger)' : d.remaining_life < 30 ? 'var(--warning)' : 'var(--success)';
                    life = `<span style="color:${lifeColor}"><i class="fas fa-heartbeat"></i> ${d.remaining_life}%</span>`;
                }
                const hours = d.power_on_hours > 0 ? Math.round(d.power_on_hours) + 'h' : '—';

                return `
                <div class="dash-list-item" style="display:flex;align-items:center;padding:12px;border-bottom:1px solid var(--border);gap:12px">
                    <div class="dash-list-icon" style="color:${statusColor};font-size:1.5em;width:30px;text-align:center"><i class="fas ${icon}"></i></div>
                    <div class="dash-list-content" style="flex:1">
                        <div class="dash-list-title" style="font-weight:600;display:flex;align-items:center;gap:8px">
                            ${d.model || d.device}
                            <span class="app-badge" style="background:${statusColor};color:#fff;font-size:0.7em;padding:1px 6px;border-radius:4px">${d.health}</span>
                        </div>
                        <div class="dash-list-subtitle" style="font-size:0.85em;color:var(--text-muted);margin-top:2px">${d.device} · ${d.serial}</div>
                    </div>
                    <div class="dash-list-end" style="display:flex;gap:15px;align-items:center;font-size:0.9em;color:var(--text)">
                         <div class="dash-chip" style="color:${tempColor}" title="${t('Temperatura')}"><i class="fas fa-thermometer-half"></i> ${temp}</div>
                         ${life ? `<div class="dash-chip" title="${t('Pozostała żywotność')}">${life}</div>` : ''}
                         <div class="dash-chip" style="color:var(--text-muted)" title="${t('Czas pracy')}"><i class="fas fa-clock"></i> ${hours}</div>
                    </div>
                </div>`;
            }).join('');
        } catch {
            // ignore
        }
    }

    // Realtime updates via Socket.IO
    function updateFromSocket() {
        if (!NAS.socket) return;
        NAS.socket.on('system_stats', (data) => {
            // Only update if dashboard window still exists
            if (!WM.windows.has('dashboard')) return;

            setGauge('dash-cpu-fill', 'dash-cpu-text', data.cpu, colorForPercent(data.cpu));
            setGauge('dash-ram-fill', 'dash-ram-text', data.memory_percent, colorForPercent(data.memory_percent));

            const ramDetail = body.querySelector('#dash-ram-detail');
            if (ramDetail) ramDetail.textContent = `${formatBytes(data.memory_used)} / ${formatBytes(data.memory_total)}`;

            const netUp = body.querySelector('#dash-net-up');
            const netDown = body.querySelector('#dash-net-down');
            if (netUp) netUp.textContent = formatSpeed(data.net_up);
            if (netDown) netDown.textContent = formatSpeed(data.net_down);
        });
    }

    loadSystemInfo();
    loadDockerSummary();
    loadSmartInfo();
    updateFromSocket();

    // Refresh button
    const refreshBtn = body.querySelector('#dash-refresh-btn');
    if (refreshBtn) {
        refreshBtn.addEventListener('click', async () => {
            refreshBtn.classList.add('dash-spin');
            refreshBtn.disabled = true;
            await Promise.all([loadSystemInfo(), loadDockerSummary(), loadSmartInfo()]);
            setTimeout(() => { refreshBtn.classList.remove('dash-spin'); refreshBtn.disabled = false; }, 600);
        });
    }

    // Refresh disks & docker every 30s
    const interval = setInterval(() => {
        if (!WM.windows.has('dashboard')) { clearInterval(interval); return; }
        loadSystemInfo();
        loadDockerSummary();
    }, 30000);
}


// ═══════════════════════════════════════════════════════════
//  DOCKER MANAGER (Portainer-like)
// ═══════════════════════════════════════════════════════════

AppRegistry['docker-manager'] = function (appDef) {
    createWindow('docker-manager', {
        title: t('Docker'),
        icon: appDef.icon,
        iconColor: appDef.color,
        width: 1100,
        height: 700,
        onRender: (body) => renderDockerManager(body),
    });
};

function renderDockerManager(body) {
    const isAdmin = NAS.user?.role === 'admin';
    const S = {
        tab: 'containers',
        containers: [],
        projects: [],
        images: [],
        systemInfo: null,
        filter: '',
        selectedContainer: null,
        detailTab: 'logs',
        _intervals: [],
    };

    // Helper: track intervals and clear stale ones
    function addInterval(id) { S._intervals.push(id); }
    function clearAllIntervals() { S._intervals.forEach(clearInterval); S._intervals.length = 0; }

    // Helper: escape HTML to prevent XSS from Docker names
    function esc(s) { const d = document.createElement('div'); d.textContent = s || ''; return d.innerHTML; }

    // Helper: check if env var name looks sensitive
    function isSensitiveEnv(name) {
        return /password|secret|key|token|api_key|apikey|private|credential/i.test(name);
    }

    // Helper: basic YAML syntax validation (checks structure, not full parse)
    function validateYaml(text) {
        if (!text || !text.trim()) return { valid: false, error: t('Pusta treść') };
        const lines = text.split('\n');
        let hasServices = false;
        let inBlock = false;
        const warnings = [];
        for (const line of lines) {
            const trimmed = line.trim();
            if (trimmed.startsWith('#') || !trimmed) continue;
            // Check for tabs (YAML forbids tabs for indentation)
            if (/^\t/.test(line)) return { valid: false, error: t('YAML nie może używać tabulatorów — użyj spacji') };
            // Detect services key
            if (/^services\s*:/.test(trimmed)) hasServices = true;
            // Detect dangerous directives
            if (/^\s*privileged\s*:\s*true/i.test(line)) warnings.push(t('Tryb privileged — pełny dostęp do hosta'));
            if (/^\s*network_mode\s*:\s*["']?host["']?/i.test(line)) warnings.push(t('network_mode: host — kontener widzi sieć hosta'));
            if (/^\s*pid\s*:\s*["']?host["']?/i.test(line)) warnings.push(t('pid: host — kontener widzi procesy hosta'));
            if (/^\s*cap_add\s*:/i.test(line)) inBlock = true;
            if (inBlock && /SYS_ADMIN|NET_ADMIN|ALL/i.test(trimmed)) warnings.push(t('Niebezpieczna capability: ') + trimmed.replace(/^-\s*/, ''));
            if (inBlock && /^\S/.test(line)) inBlock = false;
        }
        if (!hasServices) return { valid: false, error: t('Brak sekcji "services" — wymagana w docker-compose') };
        return { valid: true, warnings };
    }

    body.innerHTML = `
        <div class="dkr">
            <div class="dkr-sidebar">
                <div class="dkr-nav-item active" data-tab="containers"><i class="fas fa-box"></i> ${t('Kontenery')}</div>
                <div class="dkr-nav-item" data-tab="projects"><i class="fas fa-layer-group"></i> ${t('Projekty')}</div>
                <div class="dkr-nav-item" data-tab="images"><i class="fas fa-clone"></i> ${t('Obrazy')}</div>
                <div class="dkr-nav-item" data-tab="system"><i class="fas fa-server"></i> ${t('System')}</div>
            </div>
            <div class="dkr-main" id="dkr-main"></div>
        </div>
    `;

    // Tab navigation
    body.querySelectorAll('.dkr-nav-item').forEach(nav => {
        nav.addEventListener('click', () => {
            body.querySelectorAll('.dkr-nav-item').forEach(n => n.classList.remove('active'));
            nav.classList.add('active');
            S.tab = nav.dataset.tab;
            S.selectedContainer = null;
            S._inspectCache = null;
            clearAllIntervals();
            renderTab();
        });
    });

    const main = body.querySelector('#dkr-main');

    function renderTab() {
        switch (S.tab) {
            case 'containers': renderContainersTab(); break;
            case 'projects': renderProjectsTab(); break;
            case 'images': renderImagesTab(); break;
            case 'system': renderSystemTab(); break;
        }
    }

    // ─── CONTAINERS TAB ───
    async function loadContainers() {
        try {
            const res = await api('/docker/containers');
            // Pre-compute search string for performance
            S.containers = res.map(c => {
                c._search = (c.name + ' ' + c.image + ' ' + (c.project||'')).toLowerCase();
                return c;
            });
        } catch { toast(t('Błąd pobierania kontenerów'), 'error'); }
    }

    function renderContainersTab() {
        if (S.selectedContainer) { renderContainerDetail(); return; }
        main.innerHTML = `
            ${!isAdmin ? '<div class="dkr-readonly-notice"><i class="fas fa-info-circle"></i> ' + t('Tryb tylko do odczytu — wymagane uprawnienia administratora') + '</div>' : ''}
            <div class="dkr-toolbar">
                <span class="dkr-toolbar-title"><i class="fas fa-box"></i> ${t('Kontenery')} <span class="dkr-badge" id="dkr-cnt-count">0</span></span>
                <input class="dkr-filter" id="dkr-filter" placeholder="${t('Filtruj...')}" value="${esc(S.filter)}">
                <button class="dkr-btn" id="dkr-refresh" title="${t('Odśwież')}"><i class="fas fa-sync-alt"></i></button>
            </div>
            <div class="dkr-table-wrap">
                <table class="dkr-table">
                    <thead><tr>
                        <th class="app-col-icon"></th>
                        <th>${t('Nazwa')}</th>
                        <th>${t('Obraz')}</th>
                        <th>${t('Projekt')}</th>
                        <th>${t('Status')}</th>
                        ${isAdmin ? `<th class="app-col-actions">${t('Akcje')}</th>` : ''}
                    </tr></thead>
                    <tbody id="dkr-ct-body"></tbody>
                </table>
            </div>
        `;

        // Virtual scroll initialization
        S.virtual = { rowH: 45, padTop: 0, padBot: 0 };
        S.filtered = []; // Initialize to empty array to prevent TypeError in renderVirtualChunk
        const wrap = main.querySelector('.dkr-table-wrap');
        let ticking = false;
        wrap.addEventListener('scroll', () => {
            if (!ticking) {
                window.requestAnimationFrame(() => {
                    renderVirtualChunk();
                    ticking = false;
                });
                ticking = true;
            }
        });

        let filterDebounce;
        main.querySelector('#dkr-filter').addEventListener('input', e => {
            S.filter = e.target.value.toLowerCase();
            clearTimeout(filterDebounce);
            filterDebounce = setTimeout(fillContainersTable, 200);
        });
        main.querySelector('#dkr-refresh').addEventListener('click', async () => {
            await loadContainers();
            fillContainersTable();
        });
        loadContainers().then(fillContainersTable);
    }

    function fillContainersTable() {
        const tbody = main.querySelector('#dkr-ct-body');
        const badge = main.querySelector('#dkr-cnt-count');
        const wrap = main.querySelector('.dkr-table-wrap');
        if (!tbody) return;

        const f = S.filter;
        // Optimized filter using pre-computed _search
        S.filtered = S.containers.filter(c => !f || c._search.includes(f));
        badge.textContent = S.filtered.length;

        // Reset scroll on filter change if needed, but only if triggered by filter input
        if (wrap) wrap.scrollTop = 0;
        renderVirtualChunk();
    }

    function renderVirtualChunk() {
        const tbody = main.querySelector('#dkr-ct-body');
        const wrap = main.querySelector('.dkr-table-wrap');
        if (!tbody || !wrap) return;
        if (!S.filtered) S.filtered = []; // Safety check

        const rowH = 45; // Estimated row height
        const total = S.filtered.length;
        const viewH = wrap.clientHeight || 500;
        const scrollT = wrap.scrollTop;

        // Calculate visible range
        let start = Math.floor(scrollT / rowH);
        let end = Math.ceil((scrollT + viewH) / rowH);

        // Add buffer
        start = Math.max(0, start - 5);
        end = Math.min(total, end + 5);

        const padTop = start * rowH;
        const padBot = Math.max(0, (total - end) * rowH);

        const visible = S.filtered.slice(start, end);

        tbody.innerHTML = `
            <tr style="height:${padTop}px; border:0;"><td colspan="100" style="padding:0; border:0;"></td></tr>
            ${visible.map(c => {
                const st = c.state || 'exited';
                const isRun = st === 'running';
                const isPaused = st === 'paused';
                return `<tr class="dkr-row" data-id="${esc(c.id)}" data-name="${esc(c.name)}">
                    <td><span class="dkr-dot ${st}"></span></td>
                    <td><a class="dkr-link" data-cid="${esc(c.id)}">${esc(c.name)}</a></td>
                    <td class="dkr-muted dkr-ellipsis" title="${esc(c.image)}">${esc(c.image)}</td>
                    <td>${c.project ? `<span class="dkr-project-badge">${esc(c.project)}</span>` : '<span class="dkr-muted">—</span>'}</td>
                    <td class="dkr-status-text">${esc(c.status)}</td>
                    ${isAdmin ? `<td class="dkr-actions">
                        ${!isRun ? btn('start','fa-play',t('Uruchom'),'success') : ''}
                        ${isRun ? btn('stop','fa-stop',t('Zatrzymaj'),'warning') : ''}
                        ${isRun ? btn('restart','fa-redo',t('Restartuj'),'info') : ''}
                        ${isRun && !isPaused ? btn('pause','fa-pause',t('Wstrzymaj'),'') : ''}
                        ${isPaused ? btn('unpause','fa-play',t('Wznów'),'') : ''}
                        ${btn('remove','fa-trash',t('Usuń'),'danger')}
                    </td>` : ''}
                </tr>`;
            }).join('')}
            <tr style="height:${padBot}px; border:0;"><td colspan="100" style="padding:0; border:0;"></td></tr>
        `;

        // Re-attach listeners
        tbody.querySelectorAll('.dkr-act-btn').forEach(b => {
            b.addEventListener('click', async (e) => {
                e.stopPropagation();
                const row = b.closest('tr');
                const id = row.dataset.id;
                const name = row.dataset.name;
                const action = b.dataset.action;
                if (action === 'remove' && !confirm(t('Usunąć kontener') + ` ${name}?`)) return;
                try {
                    await api(`/docker/containers/${id}/action`, { method: 'POST', body: { action } });
                    toast(`${name}: ${action}`, 'success');
                    setTimeout(async () => { await loadContainers(); fillContainersTable(); }, 1000);
                } catch (err) {
                    toast(`${t('Błąd:')} ${action} ${name}`, 'error');
                }
            });
        });

        tbody.querySelectorAll('.dkr-link').forEach(a => {
            a.addEventListener('click', () => {
                S.selectedContainer = a.dataset.cid;
                renderContainerDetail();
            });
        });
    }

    function btn(action, icon, title, color) {
        return `<button class="dkr-act-btn ${color}" data-action="${action}" title="${title}"><i class="fas ${icon}"></i></button>`;
    }

    // ─── CONTAINER DETAIL ───
    async function renderContainerDetail() {
        clearAllIntervals();
        const cid = S.selectedContainer;
        main.innerHTML = `
            <div class="dkr-toolbar">
                <button class="dkr-btn" id="dkr-back"><i class="fas fa-arrow-left"></i> ${t('Powrót')}</button>
                <span class="dkr-toolbar-title" id="dkr-detail-title">${t('Ładowanie...')}</span>
                <div class="dkr-detail-tabs">
                    <button class="dkr-tab-btn active" data-dt="logs">${t('Logi')}</button>
                    <button class="dkr-tab-btn" data-dt="inspect">${t('Szczegóły')}</button>
                    <button class="dkr-tab-btn" data-dt="stats">${t('Zasoby')}</button>
                </div>
            </div>
            <div class="dkr-detail-body" id="dkr-detail-body"><div class="dkr-loading"><i class="fas fa-spinner fa-spin"></i> ${t('Ładowanie...')}</div></div>
        `;
        main.querySelector('#dkr-back').addEventListener('click', () => {
            S.selectedContainer = null;
            S._inspectCache = null;
            clearAllIntervals();
            renderContainersTab();
        });
        main.querySelectorAll('.dkr-tab-btn').forEach(t => {
            t.addEventListener('click', () => {
                main.querySelectorAll('.dkr-tab-btn').forEach(x => x.classList.remove('active'));
                t.classList.add('active');
                S.detailTab = t.dataset.dt;
                clearAllIntervals(); // stop previous sub-tab timers
                renderDetailContent(cid);
            });
        });
        // Load inspect for title
        try {
            const info = await api(`/docker/containers/${cid}/inspect`);
            main.querySelector('#dkr-detail-title').innerHTML = `<span class="dkr-dot ${info.state.status}"></span> ${esc(info.name)}`;
            S._inspectCache = info;
        } catch { }
        S.detailTab = 'logs';
        renderDetailContent(cid);
    }

    async function renderDetailContent(cid) {
        const db = main.querySelector('#dkr-detail-body');
        if (!db) return;
        switch (S.detailTab) {
            case 'logs': await renderLogsPanel(db, cid); break;
            case 'inspect': await renderInspectPanel(db, cid); break;
            case 'stats': await renderStatsPanel(db, cid); break;
        }
    }

    async function renderLogsPanel(db, cid) {
        db.innerHTML = `
            <div class="dkr-logs-toolbar">
                <select class="dkr-select" id="dkr-log-lines">
                    <option value="100">100 ${t('linii')}</option>
                    <option value="200" selected>200 ${t('linii')}</option>
                    <option value="500">500 ${t('linii')}</option>
                    <option value="1000">1000 ${t('linii')}</option>
                    <option value="5000">5000 ${t('linii')}</option>
                </select>
                <input class="dkr-filter" id="dkr-log-search" placeholder="${t('Szukaj w logach...')}">
                <button class="dkr-btn" id="dkr-log-refresh" title="${t('Odśwież')}"><i class="fas fa-sync-alt"></i></button>
                <label class="dkr-check"><input type="checkbox" id="dkr-log-follow" checked> ${t('Auto-scroll')}</label>
            </div>
            <pre class="dkr-logs" id="dkr-logs"></pre>
        `;
        async function loadLogs() {
            const lines = db.querySelector('#dkr-log-lines').value;
            const search = db.querySelector('#dkr-log-search').value;
            try {
                const r = await api(`/docker/containers/${cid}/logs?lines=${lines}&search=${encodeURIComponent(search)}`);
                const pre = db.querySelector('#dkr-logs');
                if (pre) {
                    pre.textContent = (r.logs || []).join('\n');
                    if (db.querySelector('#dkr-log-follow')?.checked) pre.scrollTop = pre.scrollHeight;
                }
            } catch { }
        }
        loadLogs();
        db.querySelector('#dkr-log-refresh').addEventListener('click', loadLogs);
        db.querySelector('#dkr-log-lines').addEventListener('change', loadLogs);
        let debounce;
        db.querySelector('#dkr-log-search').addEventListener('input', () => { clearTimeout(debounce); debounce = setTimeout(loadLogs, 400); });
        // Auto-refresh logs every 5s
        const iv = setInterval(() => {
            if (!WM.windows.has('docker-manager') || S.detailTab !== 'logs') { clearInterval(iv); return; }
            loadLogs();
        }, 5000);
        addInterval(iv);
    }

    async function renderInspectPanel(db, cid) {
        db.innerHTML = '<div class="dkr-loading"><i class="fas fa-spinner fa-spin"></i></div>';
        let info = S._inspectCache;
        if (!info || info.id !== cid.substring(0,12)) {
            try { info = await api(`/docker/containers/${cid}/inspect`); S._inspectCache = info; } catch { db.innerHTML = `<div class="dkr-empty">${t('Błąd')}</div>`; return; }
        }
        const s = info.state || {};
        db.innerHTML = `
            <div class="dkr-inspect">
                <div class="dkr-inspect-section">
                    <h3>${t('Ogólne')}</h3>
                    <div class="dkr-kv"><span>ID</span><span>${info.id}</span></div>
                    <div class="dkr-kv"><span>${t('Obraz')}</span><span>${info.image}</span></div>
                    <div class="dkr-kv"><span>${t('Polecenie')}</span><span><code>${info.command || info.entrypoint || '—'}</code></span></div>
                    <div class="dkr-kv"><span>${t('Utworzony')}</span><span>${info.created ? new Date(info.created).toLocaleString('pl') : '—'}</span></div>
                    <div class="dkr-kv"><span>${t('Status')}</span><span><span class="dkr-dot ${s.status}"></span> ${s.status} (PID: ${s.pid})</span></div>
                    <div class="dkr-kv"><span>${t('Polityka restartu')}</span><span>${info.restart_policy?.Name || '—'}</span></div>
                    <div class="dkr-kv"><span>${t('Tryb sieci')}</span><span>${info.network_mode}</span></div>
                    <div class="dkr-kv"><span>${t('Uprzywilejowany')}</span><span>${info.privileged ? t('Tak') : t('Nie')}</span></div>
                </div>
                ${info.ports.length ? `<div class="dkr-inspect-section"><h3>${t('Porty')}</h3>${info.ports.map(p =>
                    `<div class="dkr-kv"><span>${p.host}</span><span>→ ${p.container}</span></div>`
                ).join('')}</div>` : ''}
                ${info.mounts.length ? `<div class="dkr-inspect-section"><h3>${t('Wolumeny')}</h3>${info.mounts.map(m =>
                    `<div class="dkr-kv"><span>${m.source}</span><span>→ ${m.destination} ${m.rw ? '' : '(RO)'}</span></div>`
                ).join('')}</div>` : ''}
                ${info.networks.length ? `<div class="dkr-inspect-section"><h3>${t('Sieci')}</h3>${info.networks.map(n =>
                    `<div class="dkr-kv"><span>${n.name}</span><span>${n.ip || '—'}</span></div>`
                ).join('')}</div>` : ''}
                <div class="dkr-inspect-section">
                    <h3>${t('Zmienne środowiskowe')} <small>(${info.env.length})</small></h3>
                    <div class="dkr-env-list">${info.env.map(e => {
                        const [k,...v] = e.split('=');
                        const val = v.join('=');
                        const sensitive = isSensitiveEnv(k);
                        return `<div class="dkr-kv"><span>${esc(k)}</span><span>${sensitive
                            ? `<span class="dkr-env-masked" data-val="${esc(val)}" title="${t('Kliknij aby odsłonić')}">••••••••</span>`
                            : esc(val)
                        }</span></div>`;
                    }).join('')}</div>
                </div>
            </div>
        `;
        // Click to reveal masked env vars
        db.querySelectorAll('.dkr-env-masked').forEach(el => {
            el.addEventListener('click', () => {
                if (el.dataset.revealed === 'true') {
                    el.textContent = '••••••••';
                    el.dataset.revealed = 'false';
                } else {
                    el.textContent = el.dataset.val;
                    el.dataset.revealed = 'true';
                }
            });
        });
    }

    async function renderStatsPanel(db, cid) {
        db.innerHTML = '<div class="dkr-loading"><i class="fas fa-spinner fa-spin"></i></div>';
        async function loadStats() {
            try {
                const s = await api(`/docker/containers/${cid}/stats`);
                db.innerHTML = `
                    <div class="dkr-stats-grid">
                        <div class="dkr-stat-card"><div class="dkr-stat-icon"><i class="fas fa-microchip"></i></div><div class="dkr-stat-label">CPU</div><div class="dkr-stat-value">${s.cpu || '—'}</div></div>
                        <div class="dkr-stat-card"><div class="dkr-stat-icon"><i class="fas fa-memory"></i></div><div class="dkr-stat-label">${t('Pamięć')}</div><div class="dkr-stat-value">${s.mem || '—'}</div><div class="dkr-stat-sub">${s.mem_perc || ''}</div></div>
                        <div class="dkr-stat-card"><div class="dkr-stat-icon"><i class="fas fa-network-wired"></i></div><div class="dkr-stat-label">${t('Sieć I/O')}</div><div class="dkr-stat-value">${s.net || '—'}</div></div>
                        <div class="dkr-stat-card"><div class="dkr-stat-icon"><i class="fas fa-hdd"></i></div><div class="dkr-stat-label">Dysk I/O</div><div class="dkr-stat-value">${s.block || '—'}</div></div>
                        <div class="dkr-stat-card"><div class="dkr-stat-icon"><i class="fas fa-stream"></i></div><div class="dkr-stat-label">PID-y</div><div class="dkr-stat-value">${s.pids || '—'}</div></div>
                    </div>
                `;
            } catch { db.innerHTML = '<div class="dkr-empty">Brak danych</div>'; }
        }
        loadStats();
        const iv = setInterval(() => {
            if (!WM.windows.has('docker-manager') || S.detailTab !== 'stats') { clearInterval(iv); return; }
            loadStats();
        }, 3000);
        addInterval(iv);
    }

    // ─── PROJECTS TAB ───
    async function loadProjects() {
        try {
            const res = await api('/docker/projects');
            S.projects = res.map(p => {
                const srv = (p.containers||[]).map(c=>c.name + ' ' + (c.image||'')).join(' ');
                p._search = (p.name + ' ' + srv).toLowerCase();
                return p;
            });
        } catch { toast(t('Błąd pobierania projektów'), 'error'); }
    }

    function renderProjectsTab() {
        S.projLimit = 10;
        main.innerHTML = `
            <div class="dkr-toolbar">
                <span class="dkr-toolbar-title"><i class="fas fa-layer-group"></i> Projekty Docker Compose</span>
                <input class="dkr-filter" id="dkr-proj-filter" placeholder="Filtruj...">
                <button class="dkr-btn primary" id="dkr-proj-create"><i class="fas fa-plus"></i> Nowy projekt</button>
                <button class="dkr-btn" id="dkr-proj-refresh"><i class="fas fa-sync-alt"></i></button>
            </div>
            <div class="dkr-projects" id="dkr-projects"><div class="dkr-loading"><i class="fas fa-spinner fa-spin"></i></div></div>
        `;
        const wrap = main.querySelector('#dkr-projects');

        // Infinite scroll
        let ticking = false;
        wrap.addEventListener('scroll', () => {
             if (!ticking) {
                 window.requestAnimationFrame(() => {
                     if (wrap.scrollTop + wrap.clientHeight >= wrap.scrollHeight - 100) {
                         if (S.projLimit < (S.filteredProjects||[]).length) {
                             S.projLimit += 10;
                             fillProjects();
                         }
                     }
                     ticking = false;
                 });
                 ticking = true;
             }
        });

        main.querySelector('#dkr-proj-refresh').addEventListener('click', async () => { await loadProjects(); fillProjects(); });
        main.querySelector('#dkr-proj-create').addEventListener('click', () => openCreateProjectModal());
        main.querySelector('#dkr-proj-filter').addEventListener('input', e => {
            S.projLimit = 10;
            if (wrap) wrap.scrollTop = 0;
            fillProjects();
        });
        loadProjects().then(() => fillProjects());

        function fillProjects() {
            const wrap = main.querySelector('#dkr-projects');
            if (!wrap) return;

            const pf = (main.querySelector('#dkr-proj-filter').value || '').toLowerCase();
            S.filteredProjects = S.projects.filter(p => !pf || p._search.includes(pf));

            if (!S.filteredProjects.length) { wrap.innerHTML = `<div class="dkr-empty">${t('Brak projektów')}</div>`; return; }

            const visible = S.filteredProjects.slice(0, S.projLimit);

            wrap.innerHTML = visible.map(p => {
                const statusCls = p.status === 'running' ? 'success' : p.status === 'partial' ? 'warning' : 'muted';
                const statusLabel = p.status === 'running' ? t('Działa') : p.status === 'partial' ? t('Częściowo') : 'Zatrzymany';
                const isProt = p.protected;
                const servicesData = btoa(JSON.stringify(p.containers.map(c=>({name:c.name, service:c.service||c.name}))));
                return `
                    <div class="dkr-project-card" data-project="${esc(p.name)}">
                        <div class="dkr-project-header">
                            <div class="dkr-project-info">
                                <span class="dkr-project-name"><i class="fas fa-layer-group"></i> ${esc(p.name)}${isProt ? ' <i class="fas fa-shield-alt app-shield-icon" title="Projekt chroniony"></i>' : ''}</span>
                                <span class="dkr-project-status ${statusCls}">${statusLabel}</span>
                                <span class="dkr-muted">${p.running}/${p.total} ${t('kontenerów')}</span>
                            </div>
                            <div class="dkr-project-actions">
                                ${p.status !== 'running' ? `<button class="dkr-act-btn success" data-paction="up" title="Uruchom"><i class="fas fa-play"></i></button>` : ''}
                                ${!isProt && p.status !== 'stopped' ? `<button class="dkr-act-btn warning" data-paction="stop" title="Zatrzymaj"><i class="fas fa-stop"></i></button>` : ''}
                                <button class="dkr-act-btn info" data-paction="restart" title="Restartuj"><i class="fas fa-redo"></i></button>
                                <button class="dkr-act-btn" data-paction="pull" title="Pobierz obrazy"><i class="fas fa-download"></i></button>
                                ${!isProt ? `<button class="dkr-act-btn danger" data-paction="down" title="Down"><i class="fas fa-power-off"></i></button>` : ''}
                                <button class="dkr-compose-btn" data-project="${esc(p.name)}" title="docker-compose.yaml"><i class="fas fa-file-code"></i></button>
                                <button class="dkr-logs-btn" data-project="${esc(p.name)}" data-services="${servicesData}" title="${t('Logi kontenerów')}"><i class="fas fa-rectangle-list"></i></button>
                                ${!isProt ? `<button class="dkr-act-btn danger dkr-delete-proj-btn" data-project="${esc(p.name)}" title="${t('Usuń projekt')}"><i class="fas fa-trash-alt"></i></button>` : ''}
                            </div>
                        </div>
                        ${p.containers.length ? `<div class="dkr-project-containers">${p.containers.map(c => {
                            const ports = (c.ports || '').split(',').map(p => p.trim()).filter(p => p && p.includes('->')).map(p => {
                                const m = p.match(/(\d+\.\d+\.\d+\.\d+:)?(\d+)->(\d+)\/\w+/);
                                return m ? { host: m[2], container: m[3], label: (m[1] || '') + m[2] + ':' + m[3] } : { host: null, label: p };
                            });
                            return `<div class="dkr-project-ct">
                                <span class="dkr-dot ${c.state}"></span>
                                <span class="dkr-ct-name">${esc(c.name)}</span>
                                ${ports.length ? `<span class="dkr-ct-ports">${ports.map(p => p.host ? `<a class="dkr-port-badge dkr-port-link" href="http://${location.hostname}:${p.host}" target="_blank" rel="noopener" title="${t('Otwórz')} :${p.host}">${esc(p.label)}</a>` : `<span class="dkr-port-badge">${esc(p.label)}</span>`).join('')}</span>` : ''}
                                <span class="dkr-muted">${esc(c.image)}</span>
                                <span class="dkr-status-text">${esc(c.status)}</span>
                            </div>`;
                        }).join('')}</div>` : ''}
                    </div>
                `;
            }).join('');

            // Project actions
            wrap.querySelectorAll('.dkr-act-btn[data-paction]').forEach(b => {
                b.addEventListener('click', async () => {
                    const card = b.closest('.dkr-project-card');
                    const project = card.dataset.project;
                    const action = b.dataset.paction;
                    if (action === 'down' && !(await confirmDialog(`Docker Compose Down ${t('dla projektu')} ${project}?`, ''))) return;
                    // Note: Original code had complex handling here (loading state etc).
                    // I will replicate it simplified or assume it's fine.
                    // The view showed: b.disabled = true; ... toast ...
                    // I'll try to include it.
                    b.disabled = true;
                    b.innerHTML = '<i class="fas fa-spinner fa-spin"></i>';
                    try {
                        await api(`/docker/projects/${project}/action`, { method: 'POST', body: { action } });
                        toast(`${project}: ${action} OK`, 'success');
                    } catch (err) { toast(`${project}: ${t('błąd')} ${action}`, 'error'); }
                    setTimeout(async () => { await loadProjects(); fillProjects(); }, 2000);
                });
            });

            // Compose file viewer
            wrap.querySelectorAll('.dkr-compose-btn').forEach(b => {
                b.addEventListener('click', () => {
                    openComposeEditor(b.dataset.project);
                });
            });

            // Delete project
            wrap.querySelectorAll('.dkr-delete-proj-btn').forEach(b => {
                b.addEventListener('click', async () => {
                    const project = b.dataset.project;
                    if (!await confirmDialog(t('Usunąć projekt'), t('Czy na pewno usunąć projekt') + ` <b>${project}</b>? ` + t('Tej operacji nie można cofnąć.'))) return;

                    b.disabled = true;
                    b.innerHTML = '<i class="fas fa-spinner fa-spin"></i>';
                    try {
                        await api(`/docker/projects/${project}`, { method: 'DELETE' });
                        toast(`${t('Projekt')} ${project} ${t('usunięty')}`, 'success');
                        await loadProjects();
                        fillProjects();
                    } catch (err) {
                        toast(`${t('Błąd usuwania projektu')} ${project}`, 'error');
                        b.disabled = false;
                        b.innerHTML = '<i class="fas fa-trash-alt"></i>';
                    }
                });
            });

            // Project logs viewer
            wrap.querySelectorAll('.dkr-logs-btn').forEach(b => {
                b.addEventListener('click', () => {
                    let services = [];
                    try { services = JSON.parse(atob(b.dataset.services || '')); } catch {}
                    openProjectLogs(b.dataset.project, services);
                });
            });
        }
    }

    function openProjectLogs(projectName, services) {
        const overlay = document.createElement('div');
        overlay.className = 'dkr-modal-overlay';
        const serviceOpts = services.length
            ? services.map(s => `<option value="${s.service}">${s.name} (${s.service})</option>`).join('')
            : '';
        overlay.innerHTML = `
            <div class="dkr-modal dkr-modal-logs">
                <div class="dkr-modal-header">
                    <span><i class="fas fa-rectangle-list"></i> Logi — ${projectName}</span>
                    <button class="dkr-modal-close" id="dkr-plogs-close"><i class="fas fa-times"></i></button>
                </div>
                <div class="dkr-modal-body dkr-modal-body-flex">
                    <div class="dkr-logs-toolbar">
                        <select class="dkr-select" id="dkr-plogs-service">
                            <option value="">Wszystkie kontenery</option>
                            ${serviceOpts}
                        </select>
                        <select class="dkr-select" id="dkr-plogs-lines">
                            <option value="100">100 linii</option>
                            <option value="200" selected>200 linii</option>
                            <option value="500">500 linii</option>
                            <option value="1000">1000 linii</option>
                            <option value="5000">5000 linii</option>
                        </select>
                        <input class="dkr-filter" id="dkr-plogs-search" placeholder="Szukaj w logach...">
                        <button class="dkr-btn" id="dkr-plogs-refresh"><i class="fas fa-sync-alt"></i></button>
                        <label class="dkr-check"><input type="checkbox" id="dkr-plogs-follow" checked> Auto-scroll</label>
                    </div>
                    <pre class="dkr-logs app-flex-fill" id="dkr-plogs"></pre>
                </div>
            </div>
        `;
        body.appendChild(overlay);

        async function loadProjectLogs() {
            const lines = overlay.querySelector('#dkr-plogs-lines').value;
            const search = overlay.querySelector('#dkr-plogs-search').value;
            const service = overlay.querySelector('#dkr-plogs-service').value;
            try {
                const r = await api(`/docker/projects/${projectName}/logs?lines=${lines}&search=${encodeURIComponent(search)}&service=${encodeURIComponent(service)}`);
                const pre = overlay.querySelector('#dkr-plogs');
                if (pre) {
                    pre.textContent = (r.logs || []).join('\n');
                    if (overlay.querySelector('#dkr-plogs-follow')?.checked) pre.scrollTop = pre.scrollHeight;
                }
            } catch (err) {
                const pre = overlay.querySelector('#dkr-plogs');
                if (pre) pre.textContent = t('Błąd pobierania logów');
            }
        }

        loadProjectLogs();
        overlay.querySelector('#dkr-plogs-close').addEventListener('click', () => { clearInterval(autoIv); overlay.remove(); });
        overlay.addEventListener('click', e => { if (e.target === overlay) { clearInterval(autoIv); overlay.remove(); } });
        overlay.querySelector('#dkr-plogs-refresh').addEventListener('click', loadProjectLogs);
        overlay.querySelector('#dkr-plogs-lines').addEventListener('change', loadProjectLogs);
        overlay.querySelector('#dkr-plogs-service').addEventListener('change', loadProjectLogs);
        let debounce;
        overlay.querySelector('#dkr-plogs-search').addEventListener('input', () => { clearTimeout(debounce); debounce = setTimeout(loadProjectLogs, 400); });
        // Auto-refresh every 5s
        const autoIv = setInterval(() => {
            if (!document.body.contains(overlay)) { clearInterval(autoIv); return; }
            loadProjectLogs();
        }, 5000);
    }

    function openComposeEditor(projectName) {
        // Open a modal to view/edit compose file
        const overlay = document.createElement('div');
        overlay.className = 'dkr-modal-overlay';
        overlay.innerHTML = `
            <div class="dkr-modal">
                <div class="dkr-modal-header">
                    <span><i class="fas fa-file-code"></i> ${projectName}/docker-compose.yaml</span>
                    <button class="dkr-modal-close" id="dkr-compose-close"><i class="fas fa-times"></i></button>
                </div>
                <div class="dkr-modal-body">
                    <textarea class="dkr-compose-editor" id="dkr-compose-text" spellcheck="false">${t('Ładowanie...')}</textarea>
                </div>
                <div class="dkr-modal-footer">
                    <button class="dkr-btn" id="dkr-compose-cancel">Anuluj</button>
                    <button class="dkr-btn primary" id="dkr-compose-save"><i class="fas fa-save"></i> Zapisz</button>
                </div>
            </div>
        `;
        body.appendChild(overlay);
        const textarea = overlay.querySelector('#dkr-compose-text');
        overlay.querySelector('#dkr-compose-close').addEventListener('click', () => overlay.remove());
        overlay.querySelector('#dkr-compose-cancel').addEventListener('click', () => overlay.remove());
        overlay.addEventListener('click', e => { if (e.target === overlay) overlay.remove(); });

        api(`/docker/projects/${projectName}/compose`).then(r => {
            textarea.value = r.content || '';
        }).catch(() => { textarea.value = t('# Błąd odczytu'); });

        overlay.querySelector('#dkr-compose-save').addEventListener('click', async () => {
            try {
                await api(`/docker/projects/${projectName}/compose`, {
                    method: 'PUT',
                    body: { content: textarea.value }
                });
                toast('Zapisano docker-compose.yaml', 'success');
                overlay.remove();
            } catch (err) {
                toast(err?.error || t('Błąd zapisu'), 'error');
            }
        });
    }

    function openCreateProjectModal() {
        const overlay = document.createElement('div');
        overlay.className = 'dkr-modal-overlay';
        overlay.innerHTML = `
            <div class="dkr-modal">
                <div class="dkr-modal-header">
                    <span><i class="fas fa-plus"></i> Nowy projekt Docker Compose</span>
                    <button class="dkr-modal-close" id="dkr-create-close"><i class="fas fa-times"></i></button>
                </div>
                <div class="dkr-modal-body dkr-modal-body-form">
                    <div>
                        <label class="app-form-label">${t('Nazwa projektu')}</label>
                        <input class="dkr-filter app-input-full" id="dkr-create-name" placeholder="moj-projekt">
                    </div>
                    <div class="app-flex-col">
                        <div class="app-row-between">
                            <label class="app-form-label-inline">docker-compose.yaml</label>
                            <label class="dkr-btn dkr-btn-file" id="dkr-create-file-label">
                                <i class="fas fa-file-import"></i> ${t('Wczytaj z pliku')}
                                <input type="file" id="dkr-create-file" accept=".yml,.yaml" class="hidden">
                            </label>
                        </div>
                        <textarea class="dkr-compose-editor dkr-compose-flex" id="dkr-create-content" spellcheck="false">version: '3'

services:
  app:
    image: hello-world
    restart: unless-stopped
</textarea>
                    </div>
                </div>
                <div class="dkr-modal-footer">
                    <button class="dkr-btn" id="dkr-create-cancel">${t('Anuluj')}</button>
                    <button class="dkr-btn primary" id="dkr-create-save"><i class="fas fa-plus"></i> ${t('Utwórz')}</button>
                </div>
            </div>
        `;
        body.appendChild(overlay);
        overlay.querySelector('#dkr-create-close').addEventListener('click', () => overlay.remove());
        overlay.querySelector('#dkr-create-cancel').addEventListener('click', () => overlay.remove());
        overlay.addEventListener('click', e => { if (e.target === overlay) overlay.remove(); });

        overlay.querySelector('#dkr-create-file').addEventListener('change', e => {
            const file = e.target.files[0];
            if (!file) return;
            const reader = new FileReader();
            reader.onload = () => {
                overlay.querySelector('#dkr-create-content').value = reader.result;
                // Auto-fill project name from filename (e.g. "nextcloud-compose.yml" → "nextcloud-compose")
                const nameInput = overlay.querySelector('#dkr-create-name');
                if (!nameInput.value.trim()) {
                    const baseName = file.name.replace(/\.(ya?ml)$/i, '').replace(/[-_]?(docker[-_]?)?compose/i, '').replace(/^[-_]+|[-_]+$/g, '');
                    if (baseName) nameInput.value = baseName;
                }
                toast(t('Plik wczytany: ') + file.name, 'success');
            };
            reader.onerror = () => toast(t('Błąd odczytu pliku'), 'error');
            reader.readAsText(file);
        });

        overlay.querySelector('#dkr-create-save').addEventListener('click', async () => {
            const name = overlay.querySelector('#dkr-create-name').value.trim();
            const content = overlay.querySelector('#dkr-create-content').value.trim();
            if (!name) { toast(t('Podaj nazwę projektu'), 'warning'); return; }
            try {
                await api('/docker/projects', {
                    method: 'POST',
                    body: { name, content }
                });
                toast(`Projekt "${name}" utworzony`, 'success');
                overlay.remove();
                await loadProjects();
                renderProjectsTab(); // re-render full tab to pick up new project
            } catch (err) {
                toast(err?.error || t('Błąd tworzenia projektu'), 'error');
            }
        });
    }

    // ─── IMAGES TAB ───
    async function loadImages() {
        try { S.images = await api('/docker/images'); } catch { toast(t('Błąd pobierania obrazów'), 'error'); }
    }

    function renderImagesTab() {
        main.innerHTML = `
            <div class="dkr-toolbar">
                <span class="dkr-toolbar-title"><i class="fas fa-clone"></i> Obrazy <span class="dkr-badge" id="dkr-img-count">0</span></span>
                <input class="dkr-filter" id="dkr-img-filter" placeholder="Filtruj...">
                <button class="dkr-btn danger" id="dkr-img-prune" title="${t('Usuń nieużywane')}"><i class="fas fa-broom"></i> ${t('Wyczyść')}</button>
                <button class="dkr-btn" id="dkr-img-refresh"><i class="fas fa-sync-alt"></i></button>
            </div>
            <div class="dkr-table-wrap">
                <table class="dkr-table">
                    <thead><tr>
                        <th>Repozytorium</th>
                        <th>Tag</th>
                        <th>ID</th>
                        <th>Rozmiar</th>
                        <th>Utworzony</th>
                        <th class="app-col-sm"></th>
                    </tr></thead>
                    <tbody id="dkr-img-body"></tbody>
                </table>
            </div>
        `;
        let imgFilter = '';
        main.querySelector('#dkr-img-filter').addEventListener('input', e => { imgFilter = e.target.value.toLowerCase(); fillImages(); });
        main.querySelector('#dkr-img-refresh').addEventListener('click', async () => { await loadImages(); fillImages(); });
        main.querySelector('#dkr-img-prune').addEventListener('click', async () => {
            if (!confirm(t('Usunąć wszystkie nieużywane obrazy?'))) return;
            try {
                const r = await api('/docker/images/prune', { method: 'POST' });
                toast(t('Wyczyszczono nieużywane obrazy'), 'success');
                await loadImages(); fillImages();
            } catch { toast(t('Błąd czyszczenia'), 'error'); }
        });
        loadImages().then(() => fillImages());

        function fillImages() {
            const tbody = main.querySelector('#dkr-img-body');
            const badge = main.querySelector('#dkr-img-count');
            if (!tbody) return;
            const filtered = S.images.filter(i =>
                !imgFilter || (i.repository||'').toLowerCase().includes(imgFilter) || (i.tag||'').toLowerCase().includes(imgFilter)
            );
            badge.textContent = filtered.length;
            tbody.innerHTML = filtered.map(i => `
                <tr>
                    <td class="dkr-ellipsis" title="${esc(i.repository)}">${esc(i.repository)}</td>
                    <td><span class="dkr-tag">${esc(i.tag)}</span></td>
                    <td class="dkr-muted">${esc((i.id||'').replace('sha256:','').substring(0,12))}</td>
                    <td>${esc(i.size)}</td>
                    <td class="dkr-muted dkr-ellipsis">${esc(i.created)}</td>
                    <td><button class="dkr-act-btn danger" data-imgdel="${esc(i.id)}" title="${t('Usuń')}"><i class="fas fa-trash"></i></button></td>
                </tr>
            `).join('');
            tbody.querySelectorAll('[data-imgdel]').forEach(b => {
                b.addEventListener('click', async () => {
                    if (!confirm(t('Usunąć ten obraz?'))) return;
                    try {
                        await api(`/docker/images/${encodeURIComponent(b.dataset.imgdel)}?force=true`, { method: 'DELETE' });
                        toast(t('Obraz usunięty'), 'success');
                        await loadImages(); fillImages();
                    } catch { toast(t('Błąd usuwania obrazu'), 'error'); }
                });
            });
        }
    }

    // ─── SYSTEM TAB ───
    function renderSystemTab() {
        main.innerHTML = '<div class="dkr-loading"><i class="fas fa-spinner fa-spin"></i></div>';
        api('/docker/system').then(info => {
            S.systemInfo = info;
            main.innerHTML = `
                <div class="dkr-toolbar">
                    <span class="dkr-toolbar-title"><i class="fas fa-server"></i> Docker System</span>
                    <button class="dkr-btn danger" id="dkr-vol-prune"><i class="fas fa-broom"></i> ${t('Wyczyść wolumeny')}</button>
                </div>
                <div class="dkr-system">
                    <div class="dkr-sys-grid">
                        <div class="dkr-sys-card">
                            <div class="dkr-sys-card-title">Docker</div>
                            <div class="dkr-kv"><span>Wersja</span><span>${info.version}</span></div>
                            <div class="dkr-kv"><span>System</span><span>${info.os}</span></div>
                            <div class="dkr-kv"><span>Kernel</span><span>${info.kernel}</span></div>
                            <div class="dkr-kv"><span>Architektura</span><span>${info.arch}</span></div>
                            <div class="dkr-kv"><span>Sterownik</span><span>${info.storage_driver}</span></div>
                        </div>
                        <div class="dkr-sys-card">
                            <div class="dkr-sys-card-title">Kontenery</div>
                            <div class="dkr-sys-big-num">${info.containers}</div>
                            <div class="dkr-sys-row">
                                <span class="dkr-sys-label success"><i class="fas fa-play"></i> ${info.containers_running} ${t('działa')}</span>
                                <span class="dkr-sys-label muted"><i class="fas fa-stop"></i> ${info.containers_stopped} zatrzym.</span>
                                <span class="dkr-sys-label warning"><i class="fas fa-pause"></i> ${info.containers_paused} wstrzym.</span>
                            </div>
                        </div>
                        <div class="dkr-sys-card">
                            <div class="dkr-sys-card-title">Obrazy</div>
                            <div class="dkr-sys-big-num">${info.images}</div>
                        </div>
                    </div>
                    ${info.disk_usage && info.disk_usage.length ? `
                        <div class="dkr-inspect-section"><h3>Wykorzystanie dysku</h3>
                        <table class="dkr-table dkr-table-compact">
                            <thead><tr><th>${t('Typ')}</th><th>${t('Całkowity')}</th><th>${t('Aktywne')}</th><th>${t('Rozmiar')}</th><th>${t('Do odzyskania')}</th></tr></thead>
                            <tbody>${info.disk_usage.map(d => `
                                <tr><td>${d.type}</td><td>${d.total}</td><td>${d.active}</td><td>${d.size}</td><td>${d.reclaimable}</td></tr>
                            `).join('')}</tbody>
                        </table></div>
                    ` : ''}
                </div>
            `;
            main.querySelector('#dkr-vol-prune')?.addEventListener('click', async () => {
                if (!confirm(t('Usunąć nieużywane wolumeny?'))) return;
                try {
                    await api('/docker/volumes/prune', { method: 'POST' });
                    toast('Wyczyszczono wolumeny', 'success');
                } catch { toast(t('Błąd'), 'error'); }
            });
        }).catch(() => { main.innerHTML = `<div class="dkr-empty">${t('Błąd połączenia z Dockerem')}</div>`; });
    }

    // Initial render
    renderTab();

    // Auto-refresh (containers & projects, depending on active tab)
    const refreshInterval = setInterval(() => {
        if (!WM.windows.has('docker-manager')) { clearInterval(refreshInterval); clearAllIntervals(); return; }
        if (S.tab === 'containers' && !S.selectedContainer) { loadContainers().then(fillContainersTable); }
    }, 10000);
    addInterval(refreshInterval);
}


// ═══════════════════════════════════════════════════════════
//  VM MANAGER (QEMU/KVM)
// ═══════════════════════════════════════════════════════════

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

AppRegistry['event-log'] = function (appDef) {
    createWindow('event-log', {
        title: t('Dziennik zdarzeń'),
        icon: appDef.icon,
        iconColor: appDef.color,
        width: 1000,
        height: 650,
        onRender: (body) => renderEventLog(body),
    });
};

function renderEventLog(body) {
    const state = {
        events: [],
        total: 0,
        offset: 0,
        limit: 100,
        filter: { category: '', level: '', search: '' },
        autoScroll: true,
        stats: { total: 0, by_category: {}, by_level: {} },
    };

    const CATEGORIES = {
        system: { icon: 'fa-cog', label: 'System', color: '#3b82f6' },
        files: { icon: 'fa-folder', label: 'Pliki', color: '#f59e0b' },
        backup: { icon: 'fa-shield-alt', label: 'Backup', color: '#06b6d4' },
        docker: { icon: 'fa-cubes', label: 'Docker', color: '#2496ed' },
        storage: { icon: 'fa-hdd', label: 'Dyski', color: '#10b981' },
        network: { icon: 'fa-network-wired', label: t('Sieć'), color: '#0ea5e9' },
        printer: { icon: 'fa-print', label: 'Druk', color: '#ef4444' },
        security: { icon: 'fa-shield-halved', label: t('Bezpieczeństwo'), color: '#a855f7' },
        error: { icon: 'fa-exclamation-triangle', label: t('Błąd'), color: '#ef4444' },
    };

    const LEVELS = {
        debug: { icon: 'fa-bug', color: '#6b7280' },
        info: { icon: 'fa-info-circle', color: '#3b82f6' },
        warning: { icon: 'fa-exclamation-triangle', color: '#f59e0b' },
        error: { icon: 'fa-times-circle', color: '#ef4444' },
    };

    body.innerHTML = `
        <div class="elog">
            <div class="elog-toolbar">
                <div class="elog-filters">
                    <select id="elog-cat-filter" class="elog-select" title="Kategoria">
                        <option value="">Wszystkie kategorie</option>
                        ${Object.entries(CATEGORIES).map(([k, v]) =>
                            `<option value="${k}">${v.label}</option>`
                        ).join('')}
                    </select>
                    <select id="elog-level-filter" class="elog-select" title="Poziom">
                        <option value="">Wszystkie poziomy</option>
                        <option value="error">${t('Błędy')}</option>
                        <option value="warning">${t('Ostrzeżenia')}</option>
                        <option value="info">Info</option>
                        <option value="debug">Debug</option>
                    </select>
                    <div class="elog-search-wrap">
                        <i class="fas fa-search"></i>
                        <input type="text" id="elog-search" class="elog-search" placeholder="Szukaj w logach…">
                    </div>
                </div>
                <div class="elog-actions">
                    <button class="elog-btn" id="elog-refresh" title="${t('Odśwież')}"><i class="fas fa-sync-alt"></i></button>
                    <button class="elog-btn elog-btn-danger" id="elog-clear" title="${t('Wyczyść dziennik')}"><i class="fas fa-trash"></i> ${t('Wyczyść')}</button>
                </div>
            </div>
            <div class="elog-stats" id="elog-stats"></div>
            <div class="elog-list" id="elog-list"></div>
            <div class="elog-statusbar" id="elog-statusbar"></div>
        </div>
    `;

    function renderStats() {
        const el = body.querySelector('#elog-stats');
        const s = state.stats;
        el.innerHTML = Object.entries(CATEGORIES).map(([key, cat]) => {
            const count = s.by_category[key] || 0;
            if (!count) return '';
            return `<span class="elog-stat-chip" data-cat="${key}" title="${cat.label}">
                <i class="fas ${cat.icon}" style="color:${cat.color}"></i> ${count}
            </span>`;
        }).join('') + `
            ${(s.by_level.error || 0) > 0 ? `<span class="elog-stat-chip elog-stat-errors"><i class="fas fa-times-circle app-icon-danger"></i> ${s.by_level.error} ${t('błędów')}</span>` : ''}
            ${(s.by_level.warning || 0) > 0 ? `<span class="elog-stat-chip elog-stat-warnings"><i class="fas fa-exclamation-triangle app-text-warn"></i> ${s.by_level.warning} ${t('ostrzeżeń')}</span>` : ''}
        `;

        // Click stat chip to filter by category
        el.querySelectorAll('.elog-stat-chip[data-cat]').forEach(chip => {
            chip.style.cursor = 'pointer';
            chip.addEventListener('click', () => {
                const cat = chip.dataset.cat;
                const sel = body.querySelector('#elog-cat-filter');
                sel.value = state.filter.category === cat ? '' : cat;
                sel.dispatchEvent(new Event('change'));
            });
        });
    }

    function renderEvents() {
        const list = body.querySelector('#elog-list');
        if (!state.events.length) {
            list.innerHTML = `<div class="elog-empty"><i class="fas fa-rectangle-list"></i><p>${t('Brak zdarzeń')}</p></div>`;
            return;
        }

        list.innerHTML = state.events.map(ev => {
            const cat = CATEGORIES[ev.category] || CATEGORIES.system;
            const lvl = LEVELS[ev.level] || LEVELS.info;
            const details = ev.details ? `<div class="elog-details">${formatDetails(ev.details)}</div>` : '';
            return `
                <div class="elog-entry elog-level-${ev.level}">
                    <div class="elog-entry-time">${ev.time}</div>
                    <span class="elog-entry-cat" style="color:${cat.color}" title="${cat.label}">
                        <i class="fas ${cat.icon}"></i>
                    </span>
                    <span class="elog-entry-level" style="color:${lvl.color}" title="${ev.level}">
                        <i class="fas ${lvl.icon}"></i>
                    </span>
                    <div class="elog-entry-msg">${escapeHtml(ev.message)}${details}</div>
                </div>
            `;
        }).join('');

        // Scroll to top (newest first)
        list.scrollTop = 0;

        // Update statusbar
        body.querySelector('#elog-statusbar').textContent =
            `${t('Wyświetlono')} ${state.events.length} ${t('z')} ${state.total} ${t('zdarzeń')}`;
    }

    function escapeHtml(str) {
        return String(str).replace(/&/g, '&amp;').replace(/</g, '&lt;').replace(/>/g, '&gt;');
    }

    function formatDetails(details) {
        if (!details || typeof details !== 'object') return '';
        const parts = [];
        for (const [key, val] of Object.entries(details)) {
            const valStr = Array.isArray(val) ? val.join(', ') : String(val);
            parts.push(`<span class="elog-detail-key">${escapeHtml(key)}:</span> ${escapeHtml(valStr)}`);
        }
        return parts.join(' · ');
    }

    async function loadEvents() {
        try {
            const params = new URLSearchParams({ limit: state.limit, offset: state.offset });
            if (state.filter.category) params.set('category', state.filter.category);
            if (state.filter.level) params.set('level', state.filter.level);
            if (state.filter.search) params.set('search', state.filter.search);

            const [evData, statsData] = await Promise.all([
                api(`/eventlog?${params}`),
                api('/eventlog/stats')
            ]);

            state.events = evData.events || [];
            state.total = evData.total || 0;
            state.stats = statsData;

            renderStats();
            renderEvents();
        } catch (e) {
            body.querySelector('#elog-list').innerHTML =
                `<div class="elog-empty"><i class="fas fa-exclamation-triangle"></i><p>${t('Błąd ładowania logów')}</p></div>`;
        }
    }

    // Filter handlers
    body.querySelector('#elog-cat-filter').addEventListener('change', (e) => {
        state.filter.category = e.target.value;
        state.offset = 0;
        loadEvents();
    });
    body.querySelector('#elog-level-filter').addEventListener('change', (e) => {
        state.filter.level = e.target.value;
        state.offset = 0;
        loadEvents();
    });

    let searchTimer;
    body.querySelector('#elog-search').addEventListener('input', (e) => {
        clearTimeout(searchTimer);
        searchTimer = setTimeout(() => {
            state.filter.search = e.target.value;
            state.offset = 0;
            loadEvents();
        }, 300);
    });

    body.querySelector('#elog-refresh').addEventListener('click', () => loadEvents());
    body.querySelector('#elog-clear').addEventListener('click', async () => {
        if (!confirm(t('Wyczyścić cały dziennik zdarzeń?'))) return;
        await api('/eventlog/clear', { method: 'POST' });
        loadEvents();
    });

    // Real-time updates via socketio
    const onNewEvent = () => {
        // Only auto-refresh if at top (no offset, no filters)
        if (state.offset === 0) loadEvents();
    };
    if (NAS.socket) {
        NAS.socket.on('eventlog_new', onNewEvent);
    }

    // Initial load
    loadEvents();

    // Auto-refresh every 30s
    const interval = setInterval(() => {
        if (!WM.windows.has('event-log')) {
            clearInterval(interval);
            if (NAS.socket) NAS.socket.off('eventlog_new', onNewEvent);
            return;
        }
        loadEvents();
    }, 30000);
}

/* ═══════════════════════════════════════════════════════════════
   PACKAGE CENTER  — instalowanie i zarządzanie paczkami EthOS
   ═══════════════════════════════════════════════════════════════ */
AppRegistry['app-store'] = function (appDef) {
    createWindow('app-store', {
        title: t('Package Center'),
        icon: appDef.icon,
        iconColor: appDef.color,
        width: 1100, height: 720,
        onRender: (body) => renderPackageCenter(body),
    });
};

function renderPackageCenter(body) {
    /* ── state ── */
    const S = {
        catalog: [],         // optional apps from API
        core: [],            // core apps
        filtered: [],        // after filter/search
        tab: 'all',          // 'all' | 'installed' | 'available' | 'updates'
        category: 'all',
        search: '',
        detail: null,
        installing: {},      // task_id -> app_id map
        progressMap: {},     // app_id -> {stage, percent, message, status}
    };

    const CATEGORIES = ['System', 'Storage', 'Network', 'Media', 'Security', 'Tools'];

    /* ── init skeleton ── */
    body.className = 'pm-body';
    body.innerHTML = `
<div class="pm-layout">
  <aside class="pm-sidebar">
    <div class="pm-logo"><i class="fas fa-th"></i> <span>Package Center</span></div>
    <nav class="pm-nav">
      <button class="pm-nav-btn active" data-tab="all"><i class="fas fa-border-all"></i> ${t('Wszystkie')}<span class="pm-nav-badge" id="pm-badge-all"></span></button>
      <button class="pm-nav-btn" data-tab="installed"><i class="fas fa-check-circle"></i> ${t('Zainstalowane')}<span class="pm-nav-badge" id="pm-badge-inst"></span></button>
      <button class="pm-nav-btn" data-tab="available"><i class="fas fa-download"></i> ${t('Dostępne')}<span class="pm-nav-badge" id="pm-badge-avail"></span></button>
      <button class="pm-nav-btn" data-tab="updates"><i class="fas fa-sync-alt"></i> ${t('Aktualizacje')}<span class="pm-nav-badge pm-nav-badge-warn" id="pm-badge-upd"></span></button>
    </nav>
    <div class="pm-sidebar-sep">${t('Kategorie')}</div>
    <nav class="pm-nav">
      <button class="pm-cat-btn active" data-cat="all"><i class="fas fa-border-all"></i> ${t('Wszystkie')}</button>
      ${CATEGORIES.map(c => `<button class="pm-cat-btn" data-cat="${c}"><i class="fas fa-tag"></i> ${c}</button>`).join('')}
    </nav>
    <div class="pm-sidebar-bottom">
      <button class="pm-refresh-btn" id="pm-btn-refresh"><i class="fas fa-sync-alt"></i> ${t('Odśwież katalog')}</button>
    </div>
  </aside>
  <main class="pm-main">
    <div class="pm-toolbar">
      <div class="pm-search-wrap"><i class="fas fa-search"></i><input class="pm-search" id="pm-search" placeholder="${t('Szukaj paczki…')}" type="text"></div>
    </div>
    <div class="pm-content" id="pm-content">
      <div class="pm-loading"><i class="fas fa-spinner fa-spin"></i> ${t('Ładowanie katalogu…')}</div>
    </div>
  </main>
</div>
<div class="pm-detail-overlay" id="pm-detail-overlay" style="display:none"></div>
`;

    /* ── helpers ── */
    const $ = sel => body.querySelector(sel);
    const $$ = sel => body.querySelectorAll(sel);
    const content_el = $('#pm-content');

    function applyFilter() {
        const all = [...S.core, ...S.catalog];
        S.filtered = all.filter(app => {
            if (S.category !== 'all' && app.category !== S.category && !(app.core && S.category === 'System')) return false;
            if (S.search) {
                const q = S.search.toLowerCase();
                if (!((app.name || '').toLowerCase().includes(q) ||
                      (app.description || '').toLowerCase().includes(q) ||
                      (app.id || '').toLowerCase().includes(q))) return false;
            }
            if (S.tab === 'installed') return app.installed || app.core;
            if (S.tab === 'available') return !app.installed && !app.core;
            if (S.tab === 'updates') return app.update_available;
            return true;
        });
        render();
    }

    function updateBadges() {
        const installed = S.catalog.filter(a => a.installed).length;
        const available = S.catalog.filter(a => !a.installed).length;
        const updates = S.catalog.filter(a => a.update_available).length;
        const all = S.core.length + S.catalog.length;
        const badgeAll = $('#pm-badge-all');
        const badgeInst = $('#pm-badge-inst');
        const badgeAvail = $('#pm-badge-avail');
        const badgeUpd = $('#pm-badge-upd');
        if (badgeAll) badgeAll.textContent = all || '';
        if (badgeInst) badgeInst.textContent = (installed + S.core.length) || '';
        if (badgeAvail) badgeAvail.textContent = available || '';
        if (badgeUpd) badgeUpd.textContent = updates || '';
        badgeUpd && (badgeUpd.style.display = updates ? '' : 'none');
    }

    /* ── render ── */
    function render() {
        updateBadges();
        if (!S.filtered.length) {
            const emptyMsg = S.tab === 'updates'
                ? `<i class="fas fa-check-circle" style="color:var(--green)"></i> ${t('Brak dostępnych aktualizacji')}`
                : `<i class="fas fa-box-open"></i> ${t('Brak wyników')}`;
            content_el.innerHTML = `<div class="pm-empty">${emptyMsg}</div>`;
            return;
        }

        // Group: core first if tab=all or installed, then installed optional, then available
        let groups = [];
        if (S.tab === 'all' || S.tab === 'installed') {
            const coreApps = S.filtered.filter(a => a.core);
            const instApps = S.filtered.filter(a => !a.core && a.installed);
            const availApps = S.filtered.filter(a => !a.core && !a.installed);
            if (coreApps.length) groups.push({ label: t('Wbudowane (Core)'), icon: 'fa-lock', apps: coreApps });
            if (instApps.length) groups.push({ label: t('Zainstalowane'), icon: 'fa-check-circle', apps: instApps });
            if (availApps.length && S.tab === 'all') groups.push({ label: t('Dostępne do instalacji'), icon: 'fa-download', apps: availApps });
        } else {
            groups.push({ label: null, apps: S.filtered });
        }

        content_el.innerHTML = groups.map(g => `
<div class="pm-group">
  ${g.label ? `<div class="pm-group-header"><i class="fas ${g.icon}"></i> ${g.label}</div>` : ''}
  <div class="pm-grid">
    ${g.apps.map(app => renderCard(app)).join('')}
  </div>
</div>`).join('');

        content_el.querySelectorAll('.pm-card').forEach(card => {
            const appId = card.dataset.id;
            card.querySelector('.pm-card-body')?.addEventListener('click', () => {
                const app = [...S.catalog, ...S.core].find(a => a.id === appId);
                if (app) showDetail(app);
            });
            const installBtn = card.querySelector('.pm-btn-install');
            if (installBtn) installBtn.addEventListener('click', e => { e.stopPropagation(); installApp(appId); });
            const uninstallBtn = card.querySelector('.pm-btn-uninstall');
            if (uninstallBtn) uninstallBtn.addEventListener('click', e => { e.stopPropagation(); uninstallApp(appId); });
            const updateBtn = card.querySelector('.pm-btn-update');
            if (updateBtn) updateBtn.addEventListener('click', e => { e.stopPropagation(); updateApp(appId); });
        });
    }

    function renderCard(app) {
        const prog = S.progressMap[app.id];
        const isInstalling = prog && prog.status === 'running';
        const isRestarting = prog && prog.status === 'restarting';
        const hasError = prog && prog.status === 'error';

        let actionHtml = '';
        if (app.core) {
            actionHtml = `<span class="pm-badge-core"><i class="fas fa-lock"></i> Core</span>`;
        } else if (isInstalling || isRestarting) {
            const pct = prog.percent || 0;
            const msg = isRestarting ? t('Restartowanie…') : (prog.message || t('Instalowanie…'));
            actionHtml = `<div class="pm-progress-wrap">
              <div class="pm-progress-bar"><div class="pm-progress-fill" style="width:${pct}%"></div></div>
              <div class="pm-progress-msg">${escHtml(msg)}</div>
            </div>`;
        } else if (hasError) {
            actionHtml = `<div class="pm-error-msg"><i class="fas fa-exclamation-triangle"></i> ${escHtml(prog.message)}</div>
              <button class="pm-btn-install" data-id="${app.id}">${t('Spróbuj ponownie')}</button>`;
        } else if (app.update_available) {
            actionHtml = `<button class="pm-btn-update" data-id="${app.id}"><i class="fas fa-sync-alt"></i> ${t('Aktualizuj')} ${app.version}</button>
              <button class="pm-btn-uninstall" data-id="${app.id}"><i class="fas fa-trash"></i></button>`;
        } else if (app.installed) {
            actionHtml = `<button class="pm-btn-uninstall" data-id="${app.id}"><i class="fas fa-trash"></i> ${t('Odinstaluj')}</button>`;
        } else {
            actionHtml = `<button class="pm-btn-install" data-id="${app.id}"><i class="fas fa-download"></i> ${t('Instaluj')}</button>`;
        }

        const depsHtml = app.apt_deps?.length || app.pip_deps?.length
            ? `<div class="pm-card-deps">${[...(app.apt_deps||[]), ...(app.pip_deps||[])].slice(0,3).join(', ')}${([...(app.apt_deps||[]), ...(app.pip_deps||[])].length > 3) ? '…' : ''}</div>`
            : '';

        return `<div class="pm-card ${app.installed || app.core ? 'pm-card-installed' : ''} ${app.core ? 'pm-card-core' : ''}" data-id="${app.id}">
  <div class="pm-card-body">
    <div class="pm-card-icon" style="background:${app.color || '#6b7280'}20;color:${app.color || '#6b7280'}"><i class="fas ${app.icon || 'fa-cube'}"></i></div>
    <div class="pm-card-info">
      <div class="pm-card-name">${escHtml(app.name)}</div>
      <div class="pm-card-meta">${escHtml(app.category || '')} ${app.version ? '• v' + escHtml(app.version) : ''}</div>
      <div class="pm-card-desc">${escHtml((app.description || '').substring(0, 80))}${(app.description||'').length > 80 ? '…' : ''}</div>
      ${depsHtml}
    </div>
  </div>
  <div class="pm-card-footer">${actionHtml}</div>
</div>`;
    }

    function escHtml(s) {
        return String(s || '').replace(/&/g,'&amp;').replace(/</g,'&lt;').replace(/>/g,'&gt;').replace(/"/g,'&quot;');
    }

    /* ── detail modal ── */
    function showDetail(app) {
        S.detail = app;
        const prog = S.progressMap[app.id];
        const overlay = $('#pm-detail-overlay');
        const isCore = app.core;
        const isInst = app.installed || isCore;
        const isInstalling = prog && prog.status === 'running';

        const depsApt = (app.apt_deps || []).map(d => `<span class="pm-dep-tag">${escHtml(d)}</span>`).join('');
        const depsPip = (app.pip_deps || []).map(d => `<span class="pm-dep-tag pm-dep-pip">${escHtml(d)}</span>`).join('');
        const depsSection = (depsApt || depsPip) ? `
<div class="pm-detail-section">
  <div class="pm-detail-label">${t('Zależności')}</div>
  <div class="pm-detail-deps">
    ${depsApt ? `<div><small>apt:</small> ${depsApt}</div>` : ''}
    ${depsPip ? `<div><small>pip:</small> ${depsPip}</div>` : ''}
  </div>
</div>` : '';

        let actionBtn = '';
        if (isCore) {
            actionBtn = `<span class="pm-badge-core"><i class="fas fa-lock"></i> ${t('Wbudowane — nie do odinstalowania')}</span>`;
        } else if (isInstalling) {
            actionBtn = `<span class="pm-badge-progress">${t('Instalowanie…')}</span>`;
        } else if (isInst) {
            actionBtn = `<button class="pm-btn-uninstall" onclick="document.getElementById('pm-detail-overlay').style.display='none'; window._pmUninstall('${app.id}')">${t('Odinstaluj')}</button>`;
            if (app.update_available) {
                actionBtn = `<button class="pm-btn-update" onclick="document.getElementById('pm-detail-overlay').style.display='none'; window._pmUpdate('${app.id}')">${t('Aktualizuj do')} v${escHtml(app.version)}</button> ` + actionBtn;
            }
        } else {
            actionBtn = `<button class="pm-btn-primary" onclick="document.getElementById('pm-detail-overlay').style.display='none'; window._pmInstall('${app.id}')">${t('Zainstaluj')}</button>`;
        }

        overlay.style.display = 'flex';
        overlay.innerHTML = `
<div class="pm-detail-modal" onclick="event.stopPropagation()">
  <div class="pm-detail-header">
    <div class="pm-detail-icon" style="background:${app.color || '#6b7280'}20;color:${app.color || '#6b7280'}"><i class="fas ${app.icon || 'fa-cube'} fa-2x"></i></div>
    <div class="pm-detail-title">
      <h2>${escHtml(app.name)}</h2>
      <div class="pm-detail-sub">${escHtml(app.category || '')} ${app.version ? '• v' + escHtml(app.version) : ''} ${isCore ? '<span class="pm-badge-core-sm">Core</span>' : ''}</div>
    </div>
    <button class="pm-detail-close" onclick="document.getElementById('pm-detail-overlay').style.display='none'"><i class="fas fa-times"></i></button>
  </div>
  <div class="pm-detail-body">
    <div class="pm-detail-section">
      <div class="pm-detail-desc">${escHtml(app.description || '')}</div>
    </div>
    ${depsSection}
    <div class="pm-detail-section">
      <div class="pm-detail-label">${t('Status')}</div>
      <div>${isCore ? `<span class="pm-status-core"><i class="fas fa-lock"></i> ${t('Wbudowane (Core)')}</span>` : isInst ? `<span class="pm-status-installed"><i class="fas fa-check"></i> ${t('Zainstalowane')} ${app.installed_version ? '(v' + escHtml(app.installed_version) + ')' : ''}</span>` : `<span class="pm-status-available"><i class="fas fa-download"></i> ${t('Dostępne do instalacji')}</span>`}</div>
    </div>
  </div>
  <div class="pm-detail-footer">${actionBtn}</div>
</div>`;
        overlay.onclick = () => { overlay.style.display = 'none'; };
    }

    // Expose to onclick handlers
    window._pmInstall = installApp;
    window._pmUninstall = uninstallApp;
    window._pmUpdate = updateApp;

    /* ── app actions ── */
    async function installApp(appId) {
        if (S.progressMap[appId]?.status === 'running') return;
        S.progressMap[appId] = { stage: 'start', percent: 5, message: t('Uruchamianie…'), status: 'running' };
        render();
        const data = await api('/app-manager/' + appId + '/install', 'POST');
        if (data.error) { toast(data.error, 'error'); delete S.progressMap[appId]; render(); }
    }

    async function uninstallApp(appId) {
        const app = [...S.catalog, ...S.core].find(a => a.id === appId);
        const nm = app ? app.name : appId;
        if (!confirm(t('Odinstalować') + ' ' + nm + '?')) return;
        S.progressMap[appId] = { stage: 'start', percent: 5, message: t('Odinstalowywanie…'), status: 'running' };
        render();
        const data = await api('/app-manager/' + appId + '/uninstall', 'POST');
        if (data.error) { toast(data.error, 'error'); delete S.progressMap[appId]; render(); }
    }

    async function updateApp(appId) {
        S.progressMap[appId] = { stage: 'start', percent: 5, message: t('Aktualizowanie…'), status: 'running' };
        render();
        const data = await api('/app-manager/' + appId + '/update', 'POST');
        if (data.error) { toast(data.error, 'error'); delete S.progressMap[appId]; render(); }
    }

    /* ── SocketIO progress ── */
    function onProgress(ev) {
        const { app_id, stage, percent, message, status } = ev;
        if (!app_id) return;
        S.progressMap[app_id] = { stage, percent, message, status };

        // Reload catalog after done/error
        if (status === 'done' || status === 'error') {
            setTimeout(() => {
                delete S.progressMap[app_id];
                loadCatalog();
            }, status === 'done' ? 3000 : 5000);
        }
        render();
    }

    if (NAS.socket) {
        NAS.socket.on('app_manager_progress', onProgress);
        body.closest('.window')?.addEventListener('window-close', () => {
            NAS.socket.off('app_manager_progress', onProgress);
        });
    }

    /* ── load & filter events ── */
    async function loadCatalog(refresh) {
        try {
            const url = refresh ? '/app-manager/catalog?refresh=1' : '/app-manager/catalog';
            const data = await api(url);
            if (data.error) { content_el.innerHTML = `<div class="pm-empty">${escHtml(data.error)}</div>`; return; }
            S.catalog = data.optional || [];
            S.core = data.core || [];
            applyFilter();
        } catch (e) {
            content_el.innerHTML = `<div class="pm-empty"><i class="fas fa-exclamation-triangle"></i> ${t('Błąd ładowania katalogu')}</div>`;
        }
    }

    $('#pm-search').addEventListener('input', e => { S.search = e.target.value; applyFilter(); });

    $$('.pm-nav-btn').forEach(btn => btn.addEventListener('click', () => {
        $$('.pm-nav-btn').forEach(b => b.classList.remove('active'));
        btn.classList.add('active');
        S.tab = btn.dataset.tab;
        applyFilter();
    }));

    $$('.pm-cat-btn').forEach(btn => btn.addEventListener('click', () => {
        $$('.pm-cat-btn').forEach(b => b.classList.remove('active'));
        btn.classList.add('active');
        S.category = btn.dataset.cat;
        applyFilter();
    }));

    $('#pm-btn-refresh').addEventListener('click', async () => {
        const btn = $('#pm-btn-refresh');
        btn.disabled = true;
        btn.innerHTML = '<i class="fas fa-spinner fa-spin"></i>';
        await loadCatalog(true);
        btn.disabled = false;
        btn.innerHTML = '<i class="fas fa-sync-alt"></i> ' + t('Odśwież katalog');
    });

    loadCatalog();
}


// ═══════════════════════════════════════════════════════════
//  REMOTE LOG (Zdalne logi)
// ═══════════════════════════════════════════════════════════

AppRegistry['remote-log'] = function (appDef) {
    createWindow('remote-log', {
        title: t('Zdalne logi'),
        icon: appDef.icon,
        iconColor: appDef.color,
        width: 700,
        height: 600,
        onRender: (body) => renderRemoteLog(body),
    });
};

function renderRemoteLog(body) {
    const CSS = `
    <style>
    .rl-wrap{padding:20px;font-size:14px;color:var(--text-primary);overflow-y:auto;height:100%}
    .rl-card{background:var(--bg-secondary);border:1px solid var(--border-color);border-radius:10px;padding:20px;margin-bottom:16px}
    .rl-card h3{margin:0 0 14px;font-size:16px;font-weight:600}
    .rl-row{display:flex;align-items:center;gap:12px;margin-bottom:12px}
    .rl-row label{min-width:140px;font-size:13px;color:var(--text-secondary)}
    .rl-input{flex:1;background:var(--bg-tertiary);border:1px solid var(--border-color);border-radius:6px;padding:8px 12px;color:var(--text-primary);font-size:13px}
    .rl-input:focus{outline:none;border-color:var(--accent)}
    .rl-toggle{position:relative;width:44px;height:24px;cursor:pointer}
    .rl-toggle input{display:none}
    .rl-toggle .slider{position:absolute;inset:0;background:var(--bg-tertiary);border-radius:12px;transition:.3s}
    .rl-toggle input:checked+.slider{background:var(--accent)}
    .rl-toggle .slider::before{content:'';position:absolute;width:18px;height:18px;left:3px;bottom:3px;background:#fff;border-radius:50%;transition:.3s}
    .rl-toggle input:checked+.slider::before{transform:translateX(20px)}
    .rl-btn{padding:8px 18px;border:none;border-radius:6px;font-size:13px;font-weight:600;cursor:pointer;transition:filter .2s}
    .rl-btn-primary{background:var(--accent);color:#fff}
    .rl-btn-primary:hover{filter:brightness(1.1)}
    .rl-btn-secondary{background:var(--bg-tertiary);color:var(--text-secondary);border:1px solid var(--border-color)}
    .rl-btn:disabled{opacity:.4;cursor:not-allowed}
    .rl-status{display:flex;gap:16px;flex-wrap:wrap;margin-top:8px}
    .rl-stat{background:var(--bg-tertiary);border-radius:8px;padding:10px 14px;text-align:center;min-width:100px}
    .rl-stat .val{font-size:18px;font-weight:700;color:var(--accent)}
    .rl-stat .lbl{font-size:11px;color:var(--text-secondary);margin-top:2px}
    .rl-tag{display:inline-block;padding:3px 8px;border-radius:4px;font-size:11px;font-weight:600;margin:2px}
    .rl-tag-on{background:rgba(16,185,129,.15);color:#10b981}
    .rl-tag-off{background:rgba(239,68,68,.15);color:#ef4444}
    .rl-cats{display:flex;flex-wrap:wrap;gap:6px;flex:1}
    .rl-cat{padding:5px 10px;border-radius:6px;font-size:12px;cursor:pointer;border:1px solid var(--border-color);background:var(--bg-tertiary);color:var(--text-secondary);transition:all .2s}
    .rl-cat.active{background:var(--accent);color:#fff;border-color:var(--accent)}
    .rl-msg{padding:10px;border-radius:6px;font-size:13px;margin-top:8px}
    .rl-msg-ok{background:rgba(16,185,129,.1);color:#10b981}
    .rl-msg-err{background:rgba(239,68,68,.1);color:#ef4444}
    .rl-preview{background:var(--bg-tertiary);border:1px solid var(--border-color);border-radius:6px;padding:12px;font-family:monospace;font-size:11px;max-height:200px;overflow:auto;white-space:pre-wrap;word-break:break-all;margin-top:8px}
    .rl-id{font-family:monospace;font-size:12px;color:var(--text-secondary);background:var(--bg-tertiary);padding:2px 6px;border-radius:4px}
    </style>`;

    body.innerHTML = CSS + `<div class="rl-wrap">
        <div class="rl-card">
            <h3><i class="fas fa-satellite-dish app-hdr-icon"></i>${t('Zdalne raportowanie logów')}</h3>
            <div id="rl-loading" class="app-empty app-empty--loading">
                <i class="fas fa-spinner fa-spin app-spinner-md"></i>
                <div class="app-mt-sm">${t('Ładowanie konfiguracji...')}</div>
            </div>
            <div id="rl-content" class="hidden"></div>
        </div>
    </div>`;

    let config = {};

    async function load() {
        try {
            config = await api('/remote-log/config');
            render();
        } catch (e) {
            body.querySelector('#rl-loading').innerHTML = `<div class="app-text-error">${t('Błąd:')} ${esc(e.message)}</div>`;
        }
    }

    function render() {
        body.querySelector('#rl-loading').style.display = 'none';
        const content = body.querySelector('#rl-content');
        content.style.display = '';

        const allCats = ['boot','services','system','errors','dmesg'];
        const activeCats = config.log_categories || allCats;
        const lastSend = config.last_send ? new Date(config.last_send * 1000).toLocaleString() : 'nigdy';

        content.innerHTML = `
            <div class="rl-status">
                <div class="rl-stat">
                    <div class="val">${config.enabled ? '<span class="rl-tag rl-tag-on">ON</span>' : '<span class="rl-tag rl-tag-off">OFF</span>'}</div>
                    <div class="lbl">Status</div>
                </div>
                <div class="rl-stat">
                    <div class="val">${config.send_count || 0}</div>
                    <div class="lbl">${t('Wysłano')}</div>
                </div>
                <div class="rl-stat">
                    <div class="val app-text-sm">${esc(lastSend)}</div>
                    <div class="lbl">Ostatnio</div>
                </div>
            </div>
            ${config.last_error ? `<div class="rl-msg rl-msg-err app-mt-md"><i class="fas fa-exclamation-triangle"></i> ${esc(config.last_error)}</div>` : ''}
            <div class="app-mt-lg">
                <div class="rl-row">
                    <label>${t('Włączone')}</label>
                    <label class="rl-toggle"><input type="checkbox" id="rl-enabled" ${config.enabled ? 'checked' : ''}><span class="slider"></span></label>
                </div>
                <div class="rl-row">
                    <label>URL serwera</label>
                    <input class="rl-input" id="rl-url" value="${esc(config.server_url || '')}">
                </div>
                <div class="rl-row">
                    <label>${t('Interwał (min)')}</label>
                    <input class="rl-input app-input-narrow" id="rl-interval" type="number" min="5" value="${config.interval_minutes || 60}">
                </div>
                <div class="rl-row">
                    <label>${t('Wyślij przy starcie')}</label>
                    <label class="rl-toggle"><input type="checkbox" id="rl-boot" ${config.send_on_boot ? 'checked' : ''}><span class="slider"></span></label>
                </div>
                <div class="rl-row">
                    <label>${t('Wyślij przy błędzie')}</label>
                    <label class="rl-toggle"><input type="checkbox" id="rl-error" ${config.send_on_error ? 'checked' : ''}><span class="slider"></span></label>
                </div>
                <div class="rl-row">
                    <label>${t('Kategorie logów')}</label>
                    <div class="rl-cats">
                        ${allCats.map(c => `<div class="rl-cat ${activeCats.includes(c) ? 'active' : ''}" data-cat="${c}">${c}</div>`).join('')}
                    </div>
                </div>
                <div class="rl-row app-mt-xs">
                    <label>Device ID</label>
                    <span class="rl-id">${esc(config.device_id || '?')}</span>
                </div>
            </div>
            <div class="app-actions">
                <button class="rl-btn rl-btn-primary" id="rl-save"><i class="fas fa-save"></i> Zapisz</button>
                <button class="rl-btn rl-btn-secondary" id="rl-send"><i class="fas fa-paper-plane"></i> ${t('Wyślij teraz')}</button>
                <button class="rl-btn rl-btn-secondary" id="rl-preview"><i class="fas fa-eye">${t('Podgląd')}</button>
            </div>
            <div id="rl-feedback"></div>
            <div id="rl-preview-box"></div>
        `;

        // Toggle categories
        content.querySelectorAll('.rl-cat').forEach(el => {
            el.addEventListener('click', () => el.classList.toggle('active'));
        });

        // Save
        content.querySelector('#rl-save').addEventListener('click', async () => {
            const cats = [...content.querySelectorAll('.rl-cat.active')].map(e => e.dataset.cat);
            const btn = content.querySelector('#rl-save');
            btn.disabled = true;
            btn.innerHTML = `<i class="fas fa-spinner fa-spin"></i> ${t('Zapisuję...')}`;
            try {
                config = await api('/remote-log/config', {
                    method: 'POST',
                    body: {
                        enabled: content.querySelector('#rl-enabled').checked,
                        server_url: content.querySelector('#rl-url').value.trim(),
                        interval_minutes: parseInt(content.querySelector('#rl-interval').value) || 60,
                        send_on_boot: content.querySelector('#rl-boot').checked,
                        send_on_error: content.querySelector('#rl-error').checked,
                        log_categories: cats,
                    }
                });
                content.querySelector('#rl-feedback').innerHTML = '<div class="rl-msg rl-msg-ok app-mt-sm"><i class="fas fa-check"></i> Zapisano</div>';
                setTimeout(() => { try { content.querySelector('#rl-feedback').innerHTML = ''; } catch(e){} }, 3000);
            } catch (e) {
                content.querySelector('#rl-feedback').innerHTML = `<div class="rl-msg rl-msg-err app-mt-sm">${esc(e.message)}</div>`;
            }
            btn.disabled = false;
            btn.innerHTML = '<i class="fas fa-save"></i> Zapisz';
        });

        // Send now
        content.querySelector('#rl-send').addEventListener('click', async () => {
            const btn = content.querySelector('#rl-send');
            btn.disabled = true;
            btn.innerHTML = `<i class="fas fa-spinner fa-spin"></i> ${t('Wysyłanie...')}`;
            try {
                const r = await api('/remote-log/send', { method: 'POST' });
                if (r.ok) {
                    content.querySelector('#rl-feedback').innerHTML = `<div class="rl-msg rl-msg-ok app-mt-sm"><i class="fas fa-check"></i> ${t('Wysłano pomyślnie')}</div>`;
                } else {
                    content.querySelector('#rl-feedback').innerHTML = `<div class="rl-msg rl-msg-err app-mt-sm">${esc(r.message)}</div>`;
                }
                setTimeout(() => { try { content.querySelector('#rl-feedback').innerHTML = ''; } catch(e){} }, 5000);
            } catch (e) {
                content.querySelector('#rl-feedback').innerHTML = `<div class="rl-msg rl-msg-err app-mt-sm">${esc(e.message)}</div>`;
            }
            btn.disabled = false;
            btn.innerHTML = `<i class="fas fa-paper-plane"></i> ${t('Wyślij teraz')}`;
        });

        // Preview
        content.querySelector('#rl-preview').addEventListener('click', async () => {
            const box = content.querySelector('#rl-preview-box');
            const btn = content.querySelector('#rl-preview');
            if (box.children.length) { box.innerHTML = ''; return; }
            btn.disabled = true;
            btn.innerHTML = '<i class="fas fa-spinner fa-spin"></i>';
            try {
                const data = await api('/remote-log/preview');
                box.innerHTML = `<div class="rl-preview">${esc(JSON.stringify(data, null, 2))}</div>`;
            } catch (e) {
                box.innerHTML = `<div class="rl-msg rl-msg-err">${esc(e.message)}</div>`;
            }
            btn.disabled = false;
            btn.innerHTML = `<i class="fas fa-eye"></i> ${t('Podgląd')}`;
        });
    }

    load();
}

// ═══════════════════════════════════════════════════════════
//  DYNAMIC DNS — MIGRATED to Domains Manager app
//  (domains.js → DDNS tab, backend still in ddns.py)
// ═══════════════════════════════════════════════════════════

/* ═══════════════════════════════════════════════════════════════════
   System Settings App
   ═══════════════════════════════════════════════════════════════════ */

AppRegistry['system-settings'] = function (appDef) {
    createWindow('system-settings', {
        title: t('Ustawienia systemowe'),
        icon: appDef.icon,
        iconColor: appDef.color,
        width: 720,
        height: 640,
        onRender: (body) => renderSystemSettings(body),
    });
};

async function renderSystemSettings(body) {
    const esc = (s) => String(s||'').replace(/&/g,'&amp;').replace(/</g,'&lt;').replace(/>/g,'&gt;').replace(/"/g,'&quot;');

    const style = document.createElement('style');
    style.textContent = `
        .ss-wrap { padding:18px; font-family:var(--font); color:var(--text); overflow-y:auto; height:100%; }

        /* Tabs */
        .ss-tabs { display:flex; gap:2px; background:var(--bg-card,#1e293b); border-radius:10px; padding:3px; margin-bottom:18px; }
        .ss-tab { flex:1; text-align:center; padding:10px 12px; border-radius:8px; font-size:12px; font-weight:500; cursor:pointer;
                   color:var(--text-muted); transition:all .15s; display:flex; align-items:center; justify-content:center; gap:6px; }
        .ss-tab:hover { color:var(--text); }
        .ss-tab.active { background:var(--bg-hover,#334155); color:var(--text); font-weight:600; }
        .ss-tab i { font-size:13px; }

        /* Section */
        .ss-section { display:none; }
        .ss-section.active { display:block; }
        .ss-section-title { font-size:14px; font-weight:600; margin:0 0 14px; display:flex; align-items:center; gap:8px; }
        .ss-section-title i { opacity:.6; font-size:13px; }

        /* Form */
        .ss-group { margin-bottom:18px; }
        .ss-group-title { font-size:12px; font-weight:600; color:var(--text-muted); text-transform:uppercase; letter-spacing:.5px; margin-bottom:10px; }
        .ss-row { display:flex; align-items:center; gap:14px; margin-bottom:12px; }
        .ss-row label { width:160px; font-size:13px; color:var(--text); flex-shrink:0; }
        .ss-row input, .ss-row select { flex:1; padding:8px 12px; border:1px solid var(--border,#334155); border-radius:8px;
                   background:var(--bg-input,#0f172a); color:var(--text); font-size:13px; outline:none; transition:border .15s; }
        .ss-row input:focus, .ss-row select:focus { border-color:#3b82f6; }
        .ss-row input[readonly] { opacity:.6; cursor:default; }
        .ss-hint { font-size:11px; color:var(--text-muted); margin:-6px 0 12px 174px; line-height:1.4; }

        /* Buttons */
        .ss-btn { display:inline-flex; align-items:center; gap:6px; padding:9px 18px; border:none; border-radius:8px;
                   font-size:12px; cursor:pointer; font-weight:500; transition:all .15s; }
        .ss-btn-primary { background:#3b82f6; color:#fff; }
        .ss-btn-primary:hover { background:#2563eb; }
        .ss-btn-danger { background:#991b1b; color:#fff; }
        .ss-btn-danger:hover { background:#7f1d1d; }
        .ss-btn-warn { background:#d97706; color:#fff; }
        .ss-btn-warn:hover { background:#b45309; }
        .ss-btn:disabled { opacity:.5; cursor:not-allowed; }
        .ss-actions { display:flex; gap:10px; margin-top:16px; }

        /* Info cards */
        .ss-info-grid { display:grid; grid-template-columns:1fr 1fr; gap:10px; }
        .ss-info-card { background:var(--bg-card,#1e293b); border-radius:10px; padding:14px 18px; }
        .ss-info-label { font-size:11px; color:var(--text-muted); text-transform:uppercase; letter-spacing:.5px; margin-bottom:4px; }
        .ss-info-value { font-size:15px; font-weight:600; }

        /* Messages */
        .ss-msg { padding:10px 14px; border-radius:8px; font-size:12px; margin-bottom:12px; display:flex; align-items:center; gap:8px; }
        .ss-msg-ok { background:rgba(34,197,94,.12); color:#22c55e; }
        .ss-msg-err { background:rgba(239,68,68,.12); color:#ef4444; }
        .ss-msg-warn { background:rgba(234,179,8,.12); color:#eab308; }

        /* Password strength */
        .ss-pw-strength { height:4px; border-radius:2px; margin:-4px 0 10px 174px; background:var(--border,#334155); overflow:hidden; }
        .ss-pw-strength-bar { height:100%; border-radius:2px; transition:width .3s, background .3s; }

        /* Factory Reset */
        .ss-reset-box { background:rgba(153,27,27,.08); border:1px solid rgba(153,27,27,.25); border-radius:12px; padding:20px; margin-top:4px; }
        .ss-reset-icon { font-size:36px; color:#ef4444; text-align:center; margin-bottom:12px; }
        .ss-reset-title { font-size:15px; font-weight:600; color:#ef4444; text-align:center; margin-bottom:8px; }
        .ss-reset-desc { font-size:12px; color:var(--text-muted); line-height:1.6; margin-bottom:14px; }
        .ss-reset-desc ul { margin:6px 0 0 18px; padding:0; }
        .ss-reset-desc ul li { margin-bottom:3px; }
        .ss-reset-opt { display:flex; align-items:center; gap:8px; font-size:12px; color:var(--text); margin-bottom:12px; }
        .ss-reset-opt input[type=checkbox] { accent-color:#3b82f6; width:16px; height:16px; }
        .ss-reset-confirm { display:flex; align-items:center; gap:10px; margin-bottom:14px; }
        .ss-reset-confirm label { font-size:12px; color:var(--text-muted); white-space:nowrap; }
        .ss-reset-confirm input { flex:1; padding:8px 12px; border:1px solid rgba(153,27,27,.4); border-radius:8px;
                   background:var(--bg-input,#0f172a); color:#ef4444; font-size:13px; font-weight:600; letter-spacing:1px;
                   outline:none; text-align:center; }
        .ss-reset-confirm input:focus { border-color:#ef4444; }
        .ss-reset-confirm input::placeholder { color:rgba(239,68,68,.3); font-weight:400; letter-spacing:0; }

        /* Language selector */
        .ss-lang-select { flex:1; position:relative; }
        .ss-lang-btn { width:100%; padding:8px 12px; border:1px solid var(--border,#334155); border-radius:8px;
                   background:var(--bg-input,#0f172a); color:var(--text); font-size:13px; cursor:pointer;
                   display:flex; align-items:center; gap:8px; transition:border .15s; }
        .ss-lang-btn:hover, .ss-lang-btn.open { border-color:#3b82f6; }
        .ss-lang-btn .ss-lang-flag { font-size:18px; line-height:1; }
        .ss-lang-btn .ss-lang-arrow { margin-left:auto; font-size:10px; opacity:.5; transition:transform .15s; }
        .ss-lang-btn.open .ss-lang-arrow { transform:rotate(180deg); }
        .ss-lang-dropdown { position:absolute; top:calc(100% + 4px); left:0; right:0; background:var(--bg-card,#1e293b);
                   border:1px solid var(--border,#334155); border-radius:10px; max-height:240px; overflow-y:auto;
                   z-index:100; box-shadow:0 8px 24px rgba(0,0,0,.4); display:none; }
        .ss-lang-dropdown.open { display:block; }
        .ss-lang-opt { display:flex; align-items:center; gap:10px; padding:8px 14px; cursor:pointer;
                   font-size:13px; color:var(--text); transition:background .1s; }
        .ss-lang-opt:first-child { border-radius:10px 10px 0 0; }
        .ss-lang-opt:last-child { border-radius:0 0 10px 10px; }
        .ss-lang-opt:hover { background:var(--bg-hover,#334155); }
        .ss-lang-opt.selected { background:rgba(59,130,246,.15); color:#60a5fa; font-weight:600; }
        .ss-lang-opt .ss-lang-flag { font-size:18px; line-height:1; }

        @media (max-width: 640px) {
            .ss-row { flex-direction:column; align-items:stretch; gap:4px; }
            .ss-row label { width:auto; }
            .ss-hint { margin-left:0; }
            .ss-info-grid { grid-template-columns:1fr; }
            .ss-pw-strength { margin-left:0; }
        }
    `;
    body.appendChild(style);

    const wrap = document.createElement('div');
    wrap.className = 'ss-wrap';
    body.appendChild(wrap);

    let settings = {};
    let timezones = [];

    async function load() {
        try {
            settings = await api('/settings/');
        } catch (e) {
            wrap.innerHTML = `<div class="ss-msg ss-msg-err"><i class="fas fa-exclamation-circle"></i> ${t('Błąd ładowania:')} ${esc(e.message)}</div>`;
            return;
        }
        render();
    }

    function render() {
        wrap.innerHTML = '';

        // Tabs
        const tabs = [
            { id: 'general', icon: 'fa-cog', label: t('Ogólne') },
            { id: 'network', icon: 'fa-network-wired', label: t('Sieć') },
            { id: 'security', icon: 'fa-shield-alt', label: t('Bezpieczeństwo') },
            { id: 'firewall', icon: 'fa-fire', label: t('Firewall') },
            { id: 'about', icon: 'fa-info-circle', label: t('O systemie') },
            { id: 'performance', icon: 'fa-tachometer-alt', label: t('Wydajność') },
            { id: 'maintenance', icon: 'fa-tools', label: t('Konserwacja') },
        ];
        const tabBar = document.createElement('div');
        tabBar.className = 'ss-tabs';
        tabs.forEach((t, i) => {
            const tab = document.createElement('div');
            tab.className = 'ss-tab' + (i === 0 ? ' active' : '');
            tab.dataset.tab = t.id;
            tab.innerHTML = `<i class="fas ${t.icon}"></i> ${t.label}`;
            tab.addEventListener('click', () => {
                tabBar.querySelectorAll('.ss-tab').forEach(x => x.classList.remove('active'));
                tab.classList.add('active');
                wrap.querySelectorAll('.ss-section').forEach(x => x.classList.remove('active'));
                wrap.querySelector(`[data-section="${t.id}"]`).classList.add('active');
            });
            tabBar.appendChild(tab);
        });
        wrap.appendChild(tabBar);

        // === General Section ===
        const generalHtml = `
            <div class="ss-section active" data-section="general">
                <div class="ss-section-title"><i class="fas fa-cog"></i> ${t('Ustawienia ogólne')}</div>
                <div class="ss-group">
                    <div class="ss-row">
                        <label>${t('Nazwa NAS')}</label>
                        <input type="text" id="ss-nas-name" value="${esc(settings.nas_name)}" maxlength="32" placeholder="EthOS">
                    </div>
                    <div class="ss-hint">${t('Wyświetlana na ekranie logowania i w pasku zadań')}</div>

                    <div class="ss-row">
                        <label>Hostname</label>
                        <input type="text" id="ss-hostname" value="${esc(settings.hostname)}" maxlength="63" placeholder="ethos">
                    </div>
                    <div class="ss-hint">${t('Nazwa sieciowa komputera (tylko litery, cyfry i myślniki)')}</div>

                    <div class="ss-row">
                        <label>${t('Strefa czasowa')}</label>
                        <select id="ss-timezone">
                            <option value="${esc(settings.timezone)}">${esc(settings.timezone)}</option>
                        </select>
                    </div>
                    <div class="ss-hint">
                        <a href="#" id="ss-load-tz" class="app-link-sm">
                            <i class="fas fa-sync-alt"></i> ${t('Załaduj pełną listę stref')}
                        </a>
                    </div>

                    <div class="ss-row">
                        <label>${t('Język interfejsu')}</label>
                        <div class="ss-lang-select">
                            <div class="ss-lang-btn" id="ss-lang-btn">
                                <span class="ss-lang-flag">${(I18n.supportedLangs[I18n.lang]||{}).flag||'🌐'}</span>
                                <span class="ss-lang-name">${(I18n.supportedLangs[I18n.lang]||{}).name||I18n.lang}</span>
                                <i class="fas fa-chevron-down ss-lang-arrow"></i>
                            </div>
                            <div class="ss-lang-dropdown" id="ss-lang-dropdown">
                                ${Object.entries(I18n.supportedLangs).map(([code, info]) =>
                                    `<div class="ss-lang-opt${code === I18n.lang ? ' selected' : ''}" data-lang="${code}">
                                        <span class="ss-lang-flag">${info.flag}</span>
                                        <span>${info.name}</span>
                                    </div>`
                                ).join('')}
                            </div>
                        </div>
                    </div>
                    <div class="ss-hint">${t('Zmiana języka odświeży cały interfejs')}</div>
                </div>

                <div class="ss-actions">
                    <button class="ss-btn ss-btn-primary" id="ss-save-general">
                        <i class="fas fa-save"></i> ${t('Zapisz zmiany')}
                    </button>
                </div>
            </div>
        `;

        // === Network Section ===
        const networkHtml = `
            <div class="ss-section" data-section="network">
                <div class="ss-section-title"><i class="fas fa-network-wired"></i> ${t('Ustawienia sieciowe')}</div>
                <div class="ss-group">
                    <div class="ss-group-title">${t('Serwer WWW')}</div>
                    <div class="ss-row">
                        <label>${t('Port serwera')}</label>
                        <input type="number" id="ss-port" value="${settings.port}" min="1" max="65535" step="1">
                    </div>
                    <div class="ss-hint">${t('Port na którym działa interfejs EthOS (domyślnie: 9000)')}</div>
                    <div class="ss-msg ss-msg-warn app-ml-0">
                        <i class="fas fa-exclamation-triangle"></i>
                        ${t('Zmiana portu wymaga restartu serwera. Po restarcie otwórz')} <strong>http://&lt;adres-ip&gt;:&lt;nowy-port&gt;</strong>
                    </div>
                </div>

                <div class="ss-actions">
                    <button class="ss-btn ss-btn-primary" id="ss-save-network">
                        <i class="fas fa-save"></i> ${t('Zapisz zmiany')}
                    </button>
                </div>
            </div>
        `;

        // === Security Section ===
        const securityHtml = `
            <div class="ss-section" data-section="security">
                <div class="ss-section-title"><i class="fas fa-shield-alt"></i> ${t('Bezpieczeństwo')}</div>
                <div class="ss-group">
                    <div class="ss-group-title">${t('Zmiana hasła')}</div>
                    <div id="ss-pw-msg"></div>
                    <div class="ss-row">
                        <label>${t('Obecne hasło')}</label>
                        <input type="password" id="ss-pw-current" placeholder="${t('Wpisz obecne hasło')}" autocomplete="current-password">
                    </div>
                    <div class="ss-row">
                        <label>${t('Nowe hasło')}</label>
                        <input type="password" id="ss-pw-new" placeholder="${t('Min. 4 znaki')}" autocomplete="new-password">
                    </div>
                    <div class="ss-pw-strength"><div class="ss-pw-strength-bar" id="ss-pw-bar"></div></div>
                    <div class="ss-row">
                        <label>${t('Powtórz nowe hasło')}</label>
                        <input type="password" id="ss-pw-confirm" placeholder="${t('Powtórz nowe hasło')}" autocomplete="new-password">
                    </div>
                </div>

                <div class="ss-actions">
                    <button class="ss-btn ss-btn-warn" id="ss-change-pw">
                        <i class="fas fa-key"></i> ${t('Zmień hasło')}
                    </button>
                </div>
            </div>
        `;

        // === About Section ===
        const uptimeStr = _ssFormatUptime(settings.uptime || 0);
        const aboutHtml = `
            <div class="ss-section" data-section="about">
                <div class="ss-section-title"><i class="fas fa-info-circle"></i> ${t('Informacje o systemie')}</div>
                <div class="ss-info-grid">
                    <div class="ss-info-card">
                        <div class="ss-info-label">${t('Nazwa NAS')}</div>
                        <div class="ss-info-value">${esc(settings.nas_name)}</div>
                    </div>
                    <div class="ss-info-card">
                        <div class="ss-info-label">Hostname</div>
                        <div class="ss-info-value">${esc(settings.hostname)}</div>
                    </div>
                    <div class="ss-info-card">
                        <div class="ss-info-label">Port</div>
                        <div class="ss-info-value">${settings.port}</div>
                    </div>
                    <div class="ss-info-card">
                        <div class="ss-info-label">${t('Strefa czasowa')}</div>
                        <div class="ss-info-value">${esc(settings.timezone)}</div>
                    </div>
                    <div class="ss-info-card">
                        <div class="ss-info-label">${t('Czas pracy')}</div>
                        <div class="ss-info-value">${uptimeStr}</div>
                    </div>
                    <div class="ss-info-card">
                        <div class="ss-info-label">Kernel</div>
                        <div class="ss-info-value app-text-sm">${esc(settings.kernel)}</div>
                    </div>
                    <div class="ss-info-card">
                        <div class="ss-info-label">${t('Architektura')}</div>
                        <div class="ss-info-value">${esc(settings.arch)}</div>
                    </div>
                    <div class="ss-info-card">
                        <div class="ss-info-label">${t('Katalog główny')}</div>
                        <div class="ss-info-value app-text-xs">${esc(settings.ethos_root)}</div>
                    </div>
                </div>
            </div>
        `;

        // === Performance Section ===
        const performanceHtml = `
            <div class="ss-section" data-section="performance">
                <div class="ss-section-title"><i class="fas fa-tachometer-alt"></i> ${t('Wydajność')}</div>
                <div class="ss-group">
                    <div class="ss-group-title">${t('Ustawienia sysctl')}</div>
                    <div class="ss-msg ss-msg-ok" style="margin-bottom:14px">
                        <i class="fas fa-check-circle"></i> ${t('Zoptymalizowane dla NAS')}
                    </div>

                    <div class="ss-info-grid" style="margin-bottom:14px">
                        <div class="ss-info-card">
                             <div class="ss-info-label">Swappiness</div>
                             <div class="ss-info-value" id="ss-sysctl-swappiness">-</div>
                        </div>
                        <div class="ss-info-card">
                             <div class="ss-info-label">Dirty Ratio</div>
                             <div class="ss-info-value" id="ss-sysctl-dirty">-</div>
                        </div>
                        <div class="ss-info-card">
                             <div class="ss-info-label">Cache Pressure</div>
                             <div class="ss-info-value" id="ss-sysctl-cache">-</div>
                        </div>
                         <div class="ss-info-card">
                             <div class="ss-info-label">TCP Read Max</div>
                             <div class="ss-info-value" id="ss-sysctl-tcp-rmem">-</div>
                        </div>
                    </div>

                    <div class="ss-actions">
                        <button class="ss-btn ss-btn-warn" id="ss-sysctl-reload">
                            <i class="fas fa-sync-alt"></i> ${t('Przeładuj ustawienia (sysctl)')}
                        </button>
                    </div>
                    <div class="ss-hint" style="margin:8px 0 0 0">${t('Użyj po ręcznej edycji plików w /etc/sysctl.d/')}</div>
                </div>
            </div>
        `;

        // === Maintenance Section ===
        const maintenanceHtml = `
            <div class="ss-section" data-section="maintenance">
                <div class="ss-section-title"><i class="fas fa-tools"></i> ${t('Konserwacja systemu')}</div>

                <div class="ss-reset-box">
                    <div class="ss-reset-icon"><i class="fas fa-exclamation-triangle"></i></div>
                    <div class="ss-reset-title">${t('Przywracanie ustawień fabrycznych')}</div>
                    <div class="ss-reset-desc">
                        ${t('Ta operacja przywróci system do stanu początkowego, jak po pierwszej instalacji. Zostaną usunięte:')}
                        <ul>
                            <li>${t('Wszystkie ustawienia i konfiguracje')}</li>
                            <li>${t('Baza użytkowników i profile')}</li>
                            <li>${t('Udziały sieciowe (Samba, NFS)')}</li>
                            <li>${t('Certyfikaty SSL i domeny')}</li>
                            <li>${t('Logi, cache i miniaturki')}</li>
                            <li>${t('Kontenerach Docker (opcjonalnie)')}</li>
                        </ul>
                    </div>

                    <div class="ss-reset-opt">
                        <input type="checkbox" id="ss-reset-keep-docker">
                        <label for="ss-reset-keep-docker">${t('Zachowaj kontenery Docker (AppStore)')}</label>
                    </div>

                    <div class="ss-reset-confirm">
                        <label>${t('Wpisz RESET aby potwierdzić:')}</label>
                        <input type="text" id="ss-reset-input" placeholder="RESET" autocomplete="off" spellcheck="false">
                    </div>

                    <div id="ss-reset-msg"></div>

                    <div class="ss-actions app-justify-center">
                        <button class="ss-btn ss-btn-danger" id="ss-factory-reset" disabled>
                            <i class="fas fa-undo-alt"></i> ${t('Przywróć ustawienia fabryczne')}
                        </button>
                    </div>
                </div>
            </div>
        `;

        // === Firewall Section ===
        const firewallHtml = `
            <div class="ss-section" data-section="firewall">
                <div class="ss-section-title"><i class="fas fa-fire"></i> ${t('Firewall (UFW)')}</div>

                <div class="ss-group">
                    <div class="ss-group-title">${t('Status')}</div>
                    <div style="display:flex; align-items:center; justify-content:space-between; flex-wrap:wrap; gap:10px">
                        <div id="fw-status-badge" style="font-size:13px; color:var(--text-muted)"><i class="fas fa-spinner fa-spin"></i> ${t('Ładowanie...')}</div>
                        <div style="display:flex; gap:8px">
                            <button class="ss-btn" id="fw-enable-btn"><i class="fas fa-play"></i> ${t('Włącz')}</button>
                            <button class="ss-btn ss-btn-warn" id="fw-disable-btn"><i class="fas fa-stop"></i> ${t('Wyłącz')}</button>
                            <button class="ss-btn" id="fw-refresh-btn"><i class="fas fa-sync-alt"></i></button>
                        </div>
                    </div>
                </div>

                <div class="ss-group" style="margin-top:14px">
                    <div class="ss-group-title" style="display:flex; justify-content:space-between; align-items:center">
                        <span>${t('Aktywne reguły')}</span>
                    </div>
                    <div id="fw-rules-list" style="font-size:13px; color:var(--text-muted)"><i class="fas fa-spinner fa-spin"></i></div>
                </div>

                <div class="ss-group" style="margin-top:14px">
                    <div class="ss-group-title">${t('Szybkie reguły')}</div>
                    <div style="display:flex; gap:8px; flex-wrap:wrap">
                        <button class="ss-btn" data-fw-quick="ssh"><i class="fas fa-terminal"></i> Allow SSH (22)</button>
                        <button class="ss-btn" data-fw-quick="samba"><i class="fas fa-folder-open"></i> Allow Samba (445)</button>
                        <button class="ss-btn" data-fw-quick="plex"><i class="fas fa-film"></i> Allow Plex (32400)</button>
                        <button class="ss-btn" data-fw-quick="ethos"><i class="fas fa-server"></i> Allow EthOS (9000)</button>
                    </div>
                </div>

                <div class="ss-group" style="margin-top:14px">
                    <div class="ss-group-title">${t('Dodaj regułę')}</div>
                    <div style="display:flex; gap:8px; flex-wrap:wrap; align-items:flex-end">
                        <div>
                            <div style="font-size:11px; color:var(--text-muted); margin-bottom:4px">${t('Port')}</div>
                            <input type="text" id="fw-port" placeholder="22" style="width:80px">
                        </div>
                        <div>
                            <div style="font-size:11px; color:var(--text-muted); margin-bottom:4px">${t('Protokół')}</div>
                            <select id="fw-proto" style="padding:8px 10px; background:var(--bg-input); border:1px solid var(--border); border-radius:6px; color:var(--text); font-size:13px">
                                <option value="tcp">TCP</option>
                                <option value="udp">UDP</option>
                                <option value="">TCP+UDP</option>
                            </select>
                        </div>
                        <div>
                            <div style="font-size:11px; color:var(--text-muted); margin-bottom:4px">${t('Źródło IP (opcjonalne)')}</div>
                            <input type="text" id="fw-from" placeholder="any / 192.168.1.0/24" style="width:180px">
                        </div>
                        <button class="ss-btn" id="fw-add-rule-btn"><i class="fas fa-plus"></i> ${t('Dodaj')}</button>
                    </div>
                    <div id="fw-rule-msg" style="margin-top:8px"></div>
                </div>

                <div class="ss-group" style="margin-top:14px">
                    <div class="ss-group-title" style="display:flex; justify-content:space-between; align-items:center">
                        <span>${t('Domyślne reguły EthOS')}</span>
                    </div>
                    <div style="font-size:12px; color:var(--text-muted); margin-bottom:10px">
                        SSH, EthOS Web, Samba, Plex — ${t('deny incoming, allow outgoing')}
                    </div>
                    <button class="ss-btn ss-btn-warn" id="fw-defaults-btn"><i class="fas fa-undo-alt"></i> ${t('Zastosuj domyślne reguły')}</button>
                </div>

                <div class="ss-group" style="margin-top:14px">
                    <div class="ss-group-title"><i class="fas fa-ban"></i> ${t('Zbanowane IP (Fail2Ban)')}</div>
                    <div id="fw-banned-list" style="font-size:13px; color:var(--text-muted)"><i class="fas fa-spinner fa-spin"></i></div>
                    <div class="ss-actions" style="margin-top:8px">
                        <button class="ss-btn" id="fw-banned-refresh"><i class="fas fa-sync-alt"></i> ${t('Odśwież')}</button>
                    </div>
                </div>
            </div>
        `;

        const container = document.createElement('div');
        container.innerHTML = generalHtml + networkHtml + securityHtml + firewallHtml + aboutHtml + performanceHtml + maintenanceHtml;
        wrap.appendChild(container);

        // -- Performance Logic --
        const reloadBtn = wrap.querySelector('#ss-sysctl-reload');
        if (reloadBtn) {
            reloadBtn.addEventListener('click', async () => {
                reloadBtn.disabled = true;
                const origHtml = reloadBtn.innerHTML;
                reloadBtn.innerHTML = '<i class="fas fa-spinner fa-spin"></i>';
                try {
                    const r = await api('/settings/sysctl/restart', { method: 'POST' });
                    toast(r.message || 'OK');
                } catch (e) {
                    toast(e.message, 'error');
                } finally {
                    reloadBtn.disabled = false;
                    reloadBtn.innerHTML = origHtml;
                    loadSysctl();
                }
            });
        }

        async function loadSysctl() {
            try {
                const data = await api('/settings/sysctl');
                const set = (id, val) => {
                    const el = wrap.querySelector(id);
                    if (el) el.innerText = val || '-';
                };
                set('#ss-sysctl-swappiness', data['vm.swappiness']);
                set('#ss-sysctl-dirty', data['vm.dirty_ratio']);
                set('#ss-sysctl-cache', data['vm.vfs_cache_pressure']);
                if (data['net.core.rmem_max']) {
                    const val = parseInt(data['net.core.rmem_max']);
                    set('#ss-sysctl-tcp-rmem', (val / 1024 / 1024).toFixed(0) + ' MB');
                }
            } catch (e) { console.error(e); }
        }
        loadSysctl();

        // -- Event: Language selector --
        const langBtn = wrap.querySelector('#ss-lang-btn');
        const langDrop = wrap.querySelector('#ss-lang-dropdown');
        if (langBtn && langDrop) {
            langBtn.addEventListener('click', (e) => {
                e.stopPropagation();
                const isOpen = langDrop.classList.toggle('open');
                langBtn.classList.toggle('open', isOpen);
            });
            langDrop.querySelectorAll('.ss-lang-opt').forEach(opt => {
                opt.addEventListener('click', async () => {
                    const lang = opt.dataset.lang;
                    if (lang === I18n.lang) { langDrop.classList.remove('open'); langBtn.classList.remove('open'); return; }
                    langBtn.innerHTML = '<i class="fas fa-spinner fa-spin"></i> ' + t('Zmiana języka…');
                    langDrop.classList.remove('open');
                    await setLanguage(lang);
                    // Re-render the entire settings window to reflect new language
                    render();
                    toast(t('Język zmieniony na: ') + (I18n.supportedLangs[lang]||{}).name, 'success');
                });
            });
            // Close dropdown on outside click
            document.addEventListener('click', (e) => {
                if (!langBtn.contains(e.target) && !langDrop.contains(e.target)) {
                    langDrop.classList.remove('open');
                    langBtn.classList.remove('open');
                }
            });
        }

        // -- Event: Load timezones --
        wrap.querySelector('#ss-load-tz')?.addEventListener('click', async (e) => {
            e.preventDefault();
            if (timezones.length === 0) {
                const link = e.currentTarget;
                link.innerHTML = `<i class="fas fa-spinner fa-spin"></i> ${t('Ładowanie…')}`;
                try {
                    timezones = await api('/settings/timezones');
                } catch (err) {
                    toast(t('Błąd ładowania stref: ') + err.message, 'error');
                    link.innerHTML = `<i class="fas fa-sync-alt"></i> ${t('Załaduj pełną listę stref')}`;
                    return;
                }
            }
            const sel = wrap.querySelector('#ss-timezone');
            const cur = settings.timezone;
            sel.innerHTML = timezones.map(tz =>
                `<option value="${esc(tz)}" ${tz === cur ? 'selected' : ''}>${esc(tz)}</option>`
            ).join('');
            toast(t('Załadowano ') + timezones.length + ' stref czasowych', 'info');
        });

        // Initial load when switching to firewall tab
        wrap.querySelectorAll('.ss-tab').forEach(t => {
            t.addEventListener('click', () => {
                if (t.dataset.tab === 'firewall') { loadFirewallStatus(); loadBannedIPs(); }
            });
        });

        // -- Firewall Logic --
        async function loadFirewallStatus() {
            const badge = wrap.querySelector('#fw-status-badge');
            const list = wrap.querySelector('#fw-rules-list');
            if (!badge) return;
            badge.innerHTML = `<i class="fas fa-spinner fa-spin"></i> ${t('Ładowanie...')}`;
            if (list) list.innerHTML = `<i class="fas fa-spinner fa-spin"></i>`;
            try {
                const r = await api('/firewall/status');
                const active = r.status === 'active';
                badge.innerHTML = active
                    ? `<span style="color:#22c55e"><i class="fas fa-check-circle"></i> ${t('Aktywny')}</span>`
                    : `<span style="color:#ef4444"><i class="fas fa-times-circle"></i> ${t('Nieaktywny')}</span>`;
                if (list) {
                    if (!r.rules || r.rules.length === 0) {
                        list.innerHTML = `<div style="color:var(--text-muted); font-size:12px; padding:8px 0">${t('Brak reguł')}</div>`;
                    } else {
                        list.innerHTML = `
                            <table style="width:100%; border-collapse:collapse; font-size:12px">
                                <thead>
                                    <tr style="color:var(--text-muted); border-bottom:1px solid var(--border)">
                                        <th style="text-align:left; padding:6px 8px">#</th>
                                        <th style="text-align:left; padding:6px 8px">${t('Port/Cel')}</th>
                                        <th style="text-align:left; padding:6px 8px">${t('Akcja')}</th>
                                        <th style="text-align:left; padding:6px 8px">${t('Kierunek')}</th>
                                        <th style="text-align:left; padding:6px 8px">${t('Źródło')}</th>
                                        <th style="padding:6px 4px"></th>
                                    </tr>
                                </thead>
                                <tbody>
                                    ${r.rules.map(rule => `
                                        <tr style="border-bottom:1px solid var(--border)">
                                            <td style="padding:6px 8px; color:var(--text-muted)">${rule.id}</td>
                                            <td style="padding:6px 8px; font-weight:500">${esc(rule.to)}</td>
                                            <td style="padding:6px 8px">
                                                <span style="color:${rule.action==='ALLOW'?'#22c55e':rule.action==='DENY'?'#ef4444':'#f59e0b'}">${esc(rule.action)}</span>
                                            </td>
                                            <td style="padding:6px 8px; color:var(--text-muted)">${esc(rule.direction)}</td>
                                            <td style="padding:6px 8px">${esc(rule.from)}</td>
                                            <td style="padding:6px 4px; text-align:right">
                                                <i class="fas fa-trash" style="cursor:pointer; color:#ef4444; opacity:0.7" title="${t('Usuń regułę')}" data-fw-delete="${rule.id}"></i>
                                            </td>
                                        </tr>
                                    `).join('')}
                                </tbody>
                            </table>`;
                        list.querySelectorAll('[data-fw-delete]').forEach(el => {
                            el.addEventListener('click', async () => {
                                const id = el.dataset.fwDelete;
                                if (!confirm(t('Usunąć regułę #') + id + '?')) return;
                                try {
                                    await api('/firewall/rules', { method: 'POST', body: { action: 'delete', id: parseInt(id) } });
                                    toast(t('Reguła usunięta'), 'success');
                                    loadFirewallStatus();
                                } catch(e) { toast(t('Błąd: ') + e.message, 'error'); }
                            });
                        });
                    }
                }
            } catch(e) {
                badge.innerHTML = `<span style="color:#ef4444"><i class="fas fa-exclamation-circle"></i> ${t('Błąd:')} ${e.message}</span>`;
                if (list) list.innerHTML = '';
            }
        }

        async function loadBannedIPs() {
            const el = wrap.querySelector('#fw-banned-list');
            if (!el) return;
            el.innerHTML = `<i class="fas fa-spinner fa-spin"></i>`;
            try {
                const r = await api('/firewall/banned');
                if (r.error) {
                    el.innerHTML = `<div style="color:var(--text-muted); font-size:12px">${t('Fail2Ban niedostępny')}: ${esc(r.error)}</div>`;
                    return;
                }
                if (!r.jails || r.jails.length === 0) {
                    el.innerHTML = `<div style="color:var(--text-muted); font-size:12px">${t('Brak danych Fail2Ban')}</div>`;
                    return;
                }
                let html = '';
                for (const jail of r.jails) {
                    html += `<div style="margin-bottom:10px">
                        <div style="font-size:11px; font-weight:600; color:var(--text-muted); margin-bottom:6px; text-transform:uppercase">${esc(jail.name)} (${jail.banned_ips.length})</div>`;
                    if (jail.banned_ips.length === 0) {
                        html += `<div style="font-size:12px; color:var(--text-muted)">${t('Brak zbanowanych IP')}</div>`;
                    } else {
                        html += `<div style="display:flex; flex-wrap:wrap; gap:6px">`;
                        jail.banned_ips.forEach(ip => {
                            html += `<span style="background:var(--bg-input); border:1px solid var(--border); padding:3px 8px; border-radius:4px; font-size:12px; display:flex; align-items:center; gap:6px">
                                ${esc(ip)}
                                <i class="fas fa-times" style="cursor:pointer; color:#ef4444; opacity:0.8" title="${t('Odblokuj')}" data-fw-unban-jail="${esc(jail.name)}" data-fw-unban-ip="${esc(ip)}"></i>
                            </span>`;
                        });
                        html += `</div>`;
                    }
                    html += `</div>`;
                }
                el.innerHTML = html;
                el.querySelectorAll('[data-fw-unban-ip]').forEach(btn => {
                    btn.addEventListener('click', async () => {
                        const ip = btn.dataset.fwUnbanIp;
                        const jail = btn.dataset.fwUnbanJail;
                        if (!confirm(t('Odblokować IP ') + ip + '?')) return;
                        try {
                            await api('/firewall/unban', { method: 'POST', body: { jail, ip } });
                            toast(t('Odblokowano ') + ip, 'success');
                            loadBannedIPs();
                        } catch(e) { toast(t('Błąd: ') + e.message, 'error'); }
                    });
                });
            } catch(e) {
                el.innerHTML = `<div style="color:var(--text-muted); font-size:12px">${t('Błąd: ')} ${e.message}</div>`;
            }
        }

        // Firewall toggle
        wrap.querySelector('#fw-enable-btn')?.addEventListener('click', async () => {
            if (!confirm(t('Włączyć firewall?'))) return;
            try {
                await api('/firewall/toggle', { method: 'POST', body: { enable: true } });
                toast(t('Firewall włączony'), 'success');
                loadFirewallStatus();
            } catch(e) { toast(t('Błąd: ') + e.message, 'error'); }
        });

        wrap.querySelector('#fw-disable-btn')?.addEventListener('click', async () => {
            if (!confirm(t('Wyłączyć firewall? Ruch sieciowy będzie niezabezpieczony.'))) return;
            try {
                await api('/firewall/toggle', { method: 'POST', body: { enable: false } });
                toast(t('Firewall wyłączony'), 'warn');
                loadFirewallStatus();
            } catch(e) { toast(t('Błąd: ') + e.message, 'error'); }
        });

        wrap.querySelector('#fw-refresh-btn')?.addEventListener('click', () => { loadFirewallStatus(); loadBannedIPs(); });
        wrap.querySelector('#fw-banned-refresh')?.addEventListener('click', loadBannedIPs);

        // Quick rules
        const quickRules = {
            ssh:   { port: '22',    proto: 'tcp', label: 'SSH' },
            samba: { port: '139,445', proto: 'tcp', label: 'Samba TCP' },
            plex:  { port: '32400', proto: 'tcp', label: 'Plex' },
            ethos: { port: '9000',  proto: 'tcp', label: 'EthOS Web' },
        };
        wrap.querySelectorAll('[data-fw-quick]').forEach(btn => {
            btn.addEventListener('click', async () => {
                const key = btn.dataset.fwQuick;
                const rule = quickRules[key];
                if (!rule) return;
                btn.disabled = true;
                try {
                    await api('/firewall/rules', { method: 'POST', body: { action: 'add', port: rule.port, proto: rule.proto } });
                    toast(t('Reguła dodana: ') + rule.label, 'success');
                    loadFirewallStatus();
                } catch(e) { toast(t('Błąd: ') + e.message, 'error'); }
                finally { btn.disabled = false; }
            });
        });

        // Add custom rule
        wrap.querySelector('#fw-add-rule-btn')?.addEventListener('click', async () => {
            const port = wrap.querySelector('#fw-port')?.value.trim();
            const proto = wrap.querySelector('#fw-proto')?.value;
            const from = wrap.querySelector('#fw-from')?.value.trim() || 'any';
            const msgEl = wrap.querySelector('#fw-rule-msg');
            if (!port) { if (msgEl) msgEl.innerHTML = `<span style="color:#ef4444">${t('Podaj port')}</span>`; return; }
            try {
                await api('/firewall/rules', { method: 'POST', body: { action: 'add', port, proto: proto || undefined, from } });
                if (msgEl) msgEl.innerHTML = `<span style="color:#22c55e">${t('Reguła dodana')}</span>`;
                wrap.querySelector('#fw-port').value = '';
                wrap.querySelector('#fw-from').value = '';
                loadFirewallStatus();
            } catch(e) { if (msgEl) msgEl.innerHTML = `<span style="color:#ef4444">${t('Błąd: ')} ${e.message}</span>`; }
        });

        // Apply defaults
        wrap.querySelector('#fw-defaults-btn')?.addEventListener('click', async () => {
            if (!confirm(t('Zastosować domyślne reguły EthOS? Obecne reguły zostaną zachowane.'))) return;
            try {
                await api('/firewall/rules', { method: 'POST', body: { action: 'reset_defaults' } });
                toast(t('Domyślne reguły zastosowane'), 'success');
                loadFirewallStatus();
            } catch(e) { toast(t('Błąd: ') + e.message, 'error'); }
        });

        // -- Event: Save general --
        wrap.querySelector('#ss-save-general')?.addEventListener('click', async (e) => {
            const btn = e.currentTarget;
            btn.disabled = true;
            btn.innerHTML = '<i class="fas fa-spinner fa-spin"></i> ' + t('Zapisywanie…');

            const payload = {
                nas_name: wrap.querySelector('#ss-nas-name').value.trim(),
                hostname: wrap.querySelector('#ss-hostname').value.trim(),
                timezone: wrap.querySelector('#ss-timezone').value,
            };

            try {
                const r = await api('/settings/', { method: 'POST', body: payload });
                if (r.ok) {
                    if (r.changes && r.changes.length) {
                        toast(r.changes.join('; '), 'success');
                    } else {
                        toast(t('Brak zmian'), 'info');
                    }
                    // Update NAS name in taskbar if changed
                    if (payload.nas_name && payload.nas_name !== settings.nas_name) {
                        NAS.nasName = payload.nas_name;
                        const nameEl = document.querySelector('.start-label, .taskbar-nas-name');
                        if (nameEl) nameEl.textContent = payload.nas_name;
                    }
                    settings = await api('/settings/');
                    render();
                } else {
                    toast(t('Błąd: ') + (r.errors || []).join('; '), 'error');
                }
            } catch (err) {
                toast(t('Błąd: ') + err.message, 'error');
            }
            btn.disabled = false;
            btn.innerHTML = `<i class="fas fa-save"></i> ${t('Zapisz zmiany')}`;
        });

        // -- Event: Save network --
        wrap.querySelector('#ss-save-network')?.addEventListener('click', async (e) => {
            const btn = e.currentTarget;
            const newPort = parseInt(wrap.querySelector('#ss-port').value);
            if (!newPort || newPort < 1 || newPort > 65535) {
                toast(t('Nieprawidłowy port (1-65535)'), 'error');
                return;
            }

            if (newPort !== settings.port) {
                // Confirm restart
                if (!confirm(t('Zmiana portu z {old} na {new} wymaga restartu serwera.').replace('{old}', settings.port).replace('{new}', newPort) + `\n\n` + t('Po restarcie otwórz:') + `\nhttp://${location.hostname}:${newPort}\n\n` + t('Kontynuować?'))) {
                    return;
                }
            }

            btn.disabled = true;
            btn.innerHTML = '<i class="fas fa-spinner fa-spin"></i> ' + t('Zapisywanie…');

            try {
                const r = await api('/settings/', { method: 'POST', body: { port: newPort } });
                if (r.ok) {
                    if (r.restart_needed) {
                        toast(t('Port zmieniony — restartowanie serwera…'), 'success');
                        // Trigger restart
                        await api('/settings/restart', { method: 'POST' });
                        // Show redirect countdown
                        let countdown = 8;
                        const msgDiv = document.createElement('div');
                        msgDiv.className = 'ss-msg ss-msg-warn';
                        msgDiv.style.marginLeft = '0';
                        msgDiv.innerHTML = `<i class="fas fa-spinner fa-spin"></i> ${t('Serwer restartuje się… Przekierowanie za')} ${countdown}s`;
                        wrap.querySelector('[data-section="network"]').appendChild(msgDiv);
                        const iv = setInterval(() => {
                            countdown--;
                            msgDiv.innerHTML = `<i class="fas fa-spinner fa-spin"></i> ${t('Serwer restartuje się… Przekierowanie za')} ${countdown}s`;
                            if (countdown <= 0) {
                                clearInterval(iv);
                                window.location.href = `http://${location.hostname}:${newPort}`;
                            }
                        }, 1000);
                    } else {
                        toast(r.changes?.join('; ') || t('Brak zmian'), r.changes?.length ? 'success' : 'info');
                    }
                } else {
                    toast(t('Błąd: ') + (r.errors || []).join('; '), 'error');
                    btn.disabled = false;
                    btn.innerHTML = `<i class="fas fa-save"></i> ${t('Zapisz zmiany')}`;
                }
            } catch (err) {
                toast(t('Błąd: ') + err.message, 'error');
                btn.disabled = false;
                btn.innerHTML = `<i class="fas fa-save"></i> ${t('Zapisz zmiany')}`;
            }
        });

        // -- Event: Password strength indicator --
        wrap.querySelector('#ss-pw-new')?.addEventListener('input', (e) => {
            const pw = e.target.value;
            const bar = wrap.querySelector('#ss-pw-bar');
            let str = 0;
            if (pw.length >= 4) str++;
            if (pw.length >= 8) str++;
            if (/[A-Z]/.test(pw) && /[a-z]/.test(pw)) str++;
            if (/[0-9]/.test(pw)) str++;
            if (/[^a-zA-Z0-9]/.test(pw)) str++;
            const pct = Math.min(str * 20, 100);
            const colors = ['#ef4444', '#ef4444', '#eab308', '#f59e0b', '#22c55e', '#22c55e'];
            bar.style.width = pct + '%';
            bar.style.background = colors[str] || '#ef4444';
        });

        // -- Event: Change password --
        wrap.querySelector('#ss-change-pw')?.addEventListener('click', async (e) => {
            const btn = e.currentTarget;
            const msgDiv = wrap.querySelector('#ss-pw-msg');
            msgDiv.innerHTML = '';

            const curPw = wrap.querySelector('#ss-pw-current').value;
            const newPw = wrap.querySelector('#ss-pw-new').value;
            const confirmPw = wrap.querySelector('#ss-pw-confirm').value;

            if (!curPw) { msgDiv.innerHTML = `<div class="ss-msg ss-msg-err"><i class="fas fa-exclamation-circle"></i> ${t('Podaj obecne hasło')}</div>`; return; }
            if (!newPw || newPw.length < 4) { msgDiv.innerHTML = `<div class="ss-msg ss-msg-err"><i class="fas fa-exclamation-circle"></i> ${t('Nowe hasło musi mieć min. 4 znaki')}</div>`; return; }
            if (newPw !== confirmPw) { msgDiv.innerHTML = `<div class="ss-msg ss-msg-err"><i class="fas fa-exclamation-circle"></i> ${t('Hasła nie są identyczne')}</div>`; return; }

            btn.disabled = true;
            btn.innerHTML = '<i class="fas fa-spinner fa-spin"></i> ' + t('Zmieniam…');

            try {
                const r = await api('/settings/change-password', {
                    method: 'POST',
                    body: { current_password: curPw, new_password: newPw }
                });
                if (r.ok) {
                    msgDiv.innerHTML = `<div class="ss-msg ss-msg-ok"><i class="fas fa-check-circle"></i> ${t('Hasło zostało zmienione')}</div>`;
                    wrap.querySelector('#ss-pw-current').value = '';
                    wrap.querySelector('#ss-pw-new').value = '';
                    wrap.querySelector('#ss-pw-confirm').value = '';
                    const bar = wrap.querySelector('#ss-pw-bar');
                    bar.style.width = '0%';
                } else {
                    msgDiv.innerHTML = `<div class="ss-msg ss-msg-err"><i class="fas fa-exclamation-circle"></i> ${esc(r.error || t('Błąd'))}</div>`;
                }
            } catch (err) {
                msgDiv.innerHTML = `<div class="ss-msg ss-msg-err"><i class="fas fa-exclamation-circle"></i> ${esc(err.message)}</div>`;
            }
            btn.disabled = false;
            btn.innerHTML = `<i class="fas fa-key"></i> ${t('Zmień hasło')}`;
        });

        // -- Event: Factory Reset confirm input --
        const resetInput = wrap.querySelector('#ss-reset-input');
        const resetBtn = wrap.querySelector('#ss-factory-reset');
        if (resetInput && resetBtn) {
            resetInput.addEventListener('input', () => {
                resetBtn.disabled = resetInput.value.trim() !== 'RESET';
            });

            resetBtn.addEventListener('click', async () => {
                const confirmed = await showPowerConfirm(
                    t('Przywracanie ustawień fabrycznych'),
                    'fa-exclamation-triangle',
                    '#ef4444'
                );
                if (!confirmed) return;

                const msgDiv = wrap.querySelector('#ss-reset-msg');
                const keepDocker = wrap.querySelector('#ss-reset-keep-docker')?.checked || false;

                resetBtn.disabled = true;
                resetBtn.innerHTML = '<i class="fas fa-spinner fa-spin"></i> ' + t('Resetowanie…');
                msgDiv.innerHTML = `<div class="ss-msg ss-msg-warn"><i class="fas fa-spinner fa-spin"></i> ${t('Trwa przywracanie ustawień fabrycznych… Nie zamykaj okna.')}</div>`;

                try {
                    const r = await api('/settings/factory-reset', {
                        method: 'POST',
                        body: { confirm: 'RESET', keep_docker: keepDocker },
                    });
                    if (r.ok) {
                        msgDiv.innerHTML = `<div class="ss-msg ss-msg-ok"><i class="fas fa-check-circle"></i> ${esc(r.message)}</div>`;
                        // Show restart overlay
                        showRestartOverlay(t('Przywracanie ustawień fabrycznych — serwer restartuje się…'));
                    } else {
                        msgDiv.innerHTML = `<div class="ss-msg ss-msg-err"><i class="fas fa-exclamation-circle"></i> ${esc(r.error || t('Błąd'))}</div>`;
                        resetBtn.disabled = false;
                        resetBtn.innerHTML = `<i class="fas fa-undo-alt"></i> ${t('Przywróć ustawienia fabryczne')}`;
                    }
                } catch (err) {
                    msgDiv.innerHTML = `<div class="ss-msg ss-msg-err"><i class="fas fa-exclamation-circle"></i> ${esc(err.message)}</div>`;
                    resetBtn.disabled = false;
                    resetBtn.innerHTML = `<i class="fas fa-undo-alt"></i> ${t('Przywróć ustawienia fabryczne')}`;
                }
            });
        }
    }

    // ═══════════════════════════════════════════════════════════
    //  SSL & Domains — MIGRATED to Domains Manager app
    //  (domains.js, /api/domains-mgr/*)
    // ═══════════════════════════════════════════════════════════

    function _ssFormatUptime(secs) {
        const d = Math.floor(secs / 86400);
        const h = Math.floor((secs % 86400) / 3600);
        const m = Math.floor((secs % 3600) / 60);
        let parts = [];
        if (d > 0) parts.push(d + 'd');
        if (h > 0) parts.push(h + 'h');
        parts.push(m + 'min');
        return parts.join(' ');
    }

    load();
}
