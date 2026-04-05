/*
 * Video Station — Synology-style video library & player for EthOS
 *
 * Endpoints
 *   GET  /api/video-station/pkg-status
 *   POST /api/video-station/install
 *   POST /api/video-station/uninstall
 *   GET  /api/video-station/folders
 *   POST /api/video-station/folders
 *   POST /api/video-station/scan
 *   POST /api/video-station/scan-stop
 *   GET  /api/video-station/scan-status
 *   GET  /api/video-station/library?offset=&limit=&sort=&q=&folder=
 *   GET  /api/video-station/recent?limit=20
 *   GET  /api/video-station/collections
 *   GET  /api/video-station/info/<vid>
 *   GET  /api/video-station/stream/<vid>   (Range support)
 *   GET  /api/video-station/thumb/<vid>
 *   POST /api/video-station/watched/<vid>
 *
 * Socket.IO events
 *   vs_scan_progress  → {running, total, processed, current_file}
 *   vs_scan_done      → {total_processed, duration}
 *   vs_install         → {stage, percent, message}
 */

AppRegistry['video-station'] = function (appDef, launchOpts) {

    /* ── state ─────────────────────────────────────────────── */
    let activeSection  = 'library';
    let libraryItems   = [];
    let libraryTotal   = 0;
    let libraryOffset  = 0;
    const PAGE_SIZE    = 60;
    let currentSort    = 'added_desc';
    let currentQuery   = '';
    let currentFolder  = '';
    let scanning       = false;
    let useTmdb        = true;
    let playerInterval = null;
    let bodyEl         = null;

    /* ── window ────────────────────────────────────────────── */
    const win = createWindow('video-station', {
        title: t('Video Station'),
        icon: appDef.icon,
        iconColor: appDef.color,
        width: 1100,
        height: 700,
        resizable: true,
        maximizable: true,
        onRender: (body) => { bodyEl = body; init(body); },
        onClose: () => cleanup(),
    });

    /* ── socket handlers (stored for cleanup) ──────────────── */
    function onScanProgress(d) {
        scanning = d.running;
        updateScanUI(d);
    }
    function onScanDone(d) {
        scanning = false;
        updateScanUI({ running: false, total: d.total_processed, processed: d.total_processed });
        toast(t('Skanowanie zakończone') + ' — ' + d.total_processed + ' ' + t('plików'), 'success');
        if (activeSection === 'library') loadLibrary();
        else if (activeSection === 'recent') loadRecent();
        else if (activeSection === 'collections') loadCollections();
    }
    function onInstallProgress(d) {
        const fill = bodyEl && bodyEl.querySelector('#vs-install-fill');
        const msg  = bodyEl && bodyEl.querySelector('#vs-install-msg');
        if (fill) fill.style.width = (d.percent || 0) + '%';
        if (msg)  msg.textContent = d.message || '';
        if (d.stage === 'done') {
            toast(t('Zainstalowano pomyślnie'), 'success');
            init(bodyEl);
        } else if (d.stage === 'error') {
            toast(d.message || t('Błąd instalacji'), 'error');
        }
    }

    NAS.socket.on('vs_scan_progress', onScanProgress);
    NAS.socket.on('vs_scan_done', onScanDone);
    NAS.socket.on('vs_install', onInstallProgress);

    function cleanup() {
        NAS.socket.off('vs_scan_progress', onScanProgress);
        NAS.socket.off('vs_scan_done', onScanDone);
        NAS.socket.off('vs_install', onInstallProgress);
        stopPlayer();
    }

    /* ── init ──────────────────────────────────────────────── */
    async function init(body) {
        body.innerHTML = '<div style="padding:32px;text-align:center;color:var(--text-muted)"><i class="fas fa-spinner fa-spin"></i></div>';
        const st = await api('/video-station/pkg-status');
        if (st.error) { body.innerHTML = '<div style="padding:32px;color:var(--danger)">' + (st.error) + '</div>'; return; }
        if (!st.installed) { showInstallUI(body, st); return; }
        scanning = !!st.scanning;
        renderApp(body, st);
    }

    /* ── install UI ────────────────────────────────────────── */
    function showInstallUI(body, st) {
        body.innerHTML = `
<style>${getCSS()}</style>
<div class="vs-install-wrap">
  <div class="vs-install-card">
    <div class="vs-install-icon"><i class="fas fa-film"></i></div>
    <h2>${t('Video Station nie jest zainstalowany')}</h2>
    <p class="vs-install-sub">${t('Video Station wymaga ffmpeg/ffprobe do indeksowania i odtwarzania filmów.')}</p>
    <div class="vs-install-deps">${(st.deps || ['ffmpeg']).join(', ')}</div>
    <button id="vs-install-btn" class="app-btn app-btn-primary">
      <i class="fas fa-download"></i> ${t('Zainstaluj')}
    </button>
    <div id="vs-install-progress" style="display:none;width:100%;margin-top:12px">
      <div class="vs-prog-bar"><div class="vs-prog-fill" id="vs-install-fill"></div></div>
      <div class="vs-prog-msg" id="vs-install-msg"></div>
    </div>
  </div>
</div>`;
        body.querySelector('#vs-install-btn').onclick = async () => {
            body.querySelector('#vs-install-btn').disabled = true;
            body.querySelector('#vs-install-progress').style.display = '';
            const res = await api('/video-station/install', { method: 'POST' });
            if (res.error) {
                toast(res.error, 'error');
                body.querySelector('#vs-install-btn').disabled = false;
                body.querySelector('#vs-install-progress').style.display = 'none';
            }
        };
    }

    /* ── main app layout ───────────────────────────────────── */
    function renderApp(body, st) {
        body.innerHTML = `
<style>${getCSS()}</style>
<div class="vs-layout">
  <div class="vs-sidebar">
    <div class="vs-nav-section">${t('Biblioteka')}</div>
    <div class="vs-nav-item active" data-section="library"><i class="fas fa-film"></i><span>${t('Wszystkie filmy')}</span></div>
    <div class="vs-nav-item" data-section="recent"><i class="fas fa-clock"></i><span>${t('Ostatnie')}</span></div>
    <div class="vs-nav-item" data-section="collections"><i class="fas fa-folder-open"></i><span>${t('Kolekcje')}</span></div>
    <div class="vs-nav-section">${t('Zarządzanie')}</div>
    <div class="vs-nav-item" data-section="folders"><i class="fas fa-cog"></i><span>${t('Foldery')}</span></div>
    <div class="vs-sidebar-stats" id="vs-sidebar-stats"></div>
  </div>
  <div class="vs-main">
    <div class="vs-toolbar" id="vs-toolbar"></div>
    <div class="vs-content" id="vs-content"></div>
  </div>
</div>
<div class="vs-player-overlay" id="vs-player-overlay" style="display:none">
  <div class="vs-player-top">
    <span class="vs-player-title" id="vs-player-title"></span>
    <button class="vs-player-close" id="vs-player-close"><i class="fas fa-times"></i></button>
  </div>
  <video id="vs-player-video" controls autoplay></video>
</div>`;

        if (st.stats) updateSidebarStats(body, st.stats);

        body.querySelectorAll('.vs-nav-item').forEach(n => {
            n.onclick = () => switchSection(n.dataset.section);
        });

        switchSection('library');
    }

    /* ── section switching ─────────────────────────────────── */
    function switchSection(id) {
        if (!bodyEl) return;
        activeSection = id;
        bodyEl.querySelectorAll('.vs-nav-item').forEach(n =>
            n.classList.toggle('active', n.dataset.section === id)
        );
        const toolbar = bodyEl.querySelector('#vs-toolbar');
        const content = bodyEl.querySelector('#vs-content');
        if (!toolbar || !content) return;

        switch (id) {
            case 'library':    renderLibraryToolbar(toolbar); loadLibrary(); break;
            case 'recent':     toolbar.innerHTML = '<div class="vs-toolbar-title">' + t('Ostatnio dodane') + '</div>'; loadRecent(); break;
            case 'collections': toolbar.innerHTML = '<div class="vs-toolbar-title">' + t('Kolekcje') + '</div>'; loadCollections(); break;
            case 'folders':    toolbar.innerHTML = '<div class="vs-toolbar-title">' + t('Foldery biblioteki') + '</div>'; loadFolders(); break;
        }
    }

    function updateSidebarStats(body, stats) {
        const el = body.querySelector('#vs-sidebar-stats');
        if (!el || !stats) return;
        el.innerHTML =
            '<div class="vs-stat-row"><span>' + t('Filmy') + '</span><span>' + (stats.total_videos || 0) + '</span></div>' +
            '<div class="vs-stat-row"><span>' + t('Rozmiar') + '</span><span>' + formatBytes(stats.total_size || 0) + '</span></div>';
    }

    /* ── library ───────────────────────────────────────────── */
    function renderLibraryToolbar(toolbar) {
        toolbar.innerHTML =
'<div class="vs-toolbar-group">' +
  '<div class="vs-search-box">' +
    '<i class="fas fa-search"></i>' +
    '<input type="text" id="vs-search" placeholder="' + t('Szukaj...') + '" value="' + escH(currentQuery) + '">' +
  '</div>' +
  '<select id="vs-sort" class="vs-select">' +
    '<option value="added_desc"' + (currentSort === 'added_desc' ? ' selected' : '') + '>' + t('Najnowsze') + '</option>' +
    '<option value="added_asc"' + (currentSort === 'added_asc' ? ' selected' : '') + '>' + t('Najstarsze') + '</option>' +
    '<option value="title_asc"' + (currentSort === 'title_asc' ? ' selected' : '') + '>' + t('Tytuł A-Z') + '</option>' +
    '<option value="title_desc"' + (currentSort === 'title_desc' ? ' selected' : '') + '>' + t('Tytuł Z-A') + '</option>' +
    '<option value="size_desc"' + (currentSort === 'size_desc' ? ' selected' : '') + '>' + t('Rozmiar ↓') + '</option>' +
    '<option value="duration_desc"' + (currentSort === 'duration_desc' ? ' selected' : '') + '>' + t('Czas ↓') + '</option>' +
  '</select>' +
'</div>' +
'<div class="vs-toolbar-group">' +
  '<div class="vs-scan-wrap" id="vs-scan-wrap">' +
    '<label class="vs-tmdb-check" title="' + t('Rozpoznaj filmy przez TMDb') + '">' +
      '<input type="checkbox" id="vs-tmdb-check"' + (useTmdb ? ' checked' : '') + '> ' +
      '<i class="fas fa-magic"></i> TMDb' +
    '</label>' +
    '<button id="vs-scan-btn" class="app-btn app-btn-sm"><i class="fas fa-sync-alt"></i> ' + t('Skanuj') + '</button>' +
    '<button id="vs-match-all-btn" class="app-btn app-btn-sm" title="' + t('Dopasuj wszystkie nierozpoznane filmy do TMDb') + '"><i class="fas fa-wand-magic-sparkles"></i> ' + t('Dopasuj') + '</button>' +
    '<div class="vs-scan-progress" id="vs-scan-bar" style="display:none">' +
      '<div class="vs-prog-bar"><div class="vs-prog-fill" id="vs-scan-fill"></div></div>' +
      '<span class="vs-scan-text" id="vs-scan-text"></span>' +
      '<button id="vs-scan-stop" class="app-btn app-btn-sm" style="color:var(--danger)"><i class="fas fa-stop"></i></button>' +
    '</div>' +
  '</div>' +
'</div>';

        let searchTimer = null;
        bodyEl.querySelector('#vs-search').oninput = (e) => {
            clearTimeout(searchTimer);
            searchTimer = setTimeout(() => { currentQuery = e.target.value; libraryOffset = 0; loadLibrary(); }, 350);
        };
        bodyEl.querySelector('#vs-sort').onchange = (e) => {
            currentSort = e.target.value; libraryOffset = 0; loadLibrary();
        };
        bodyEl.querySelector('#vs-scan-btn').onclick = startScan;
        bodyEl.querySelector('#vs-scan-stop').onclick = stopScan;
        bodyEl.querySelector('#vs-tmdb-check').onchange = (e) => { useTmdb = e.target.checked; };
        bodyEl.querySelector('#vs-match-all-btn').onclick = matchAll;

        if (scanning) checkScanStatus();
    }

    async function loadLibrary() {
        const content = bodyEl.querySelector('#vs-content');
        if (!content) return;
        content.innerHTML = '<div class="vs-loading"><i class="fas fa-spinner fa-spin"></i></div>';

        const params = new URLSearchParams({
            offset: libraryOffset, limit: PAGE_SIZE,
            sort: currentSort, q: currentQuery, folder: currentFolder,
        });
        const data = await api('/video-station/library?' + params);
        if (data.error) { content.innerHTML = '<div class="vs-empty">' + data.error + '</div>'; return; }

        libraryItems = data.items || [];
        libraryTotal = data.total || 0;

        if (!libraryItems.length) {
            content.innerHTML =
                '<div class="vs-empty"><i class="fas fa-film"></i>' +
                '<p>' + t('Brak filmów w bibliotece') + '</p>' +
                '<p class="vs-empty-sub">' + t('Dodaj foldery i uruchom skanowanie') + '</p></div>';
            return;
        }

        content.innerHTML = renderGrid(libraryItems) + renderPagination();
        attachGridEvents(content);
        attachPaginationEvents(content);
    }

    /* ── recent ────────────────────────────────────────────── */
    async function loadRecent() {
        const content = bodyEl.querySelector('#vs-content');
        if (!content) return;
        content.innerHTML = '<div class="vs-loading"><i class="fas fa-spinner fa-spin"></i></div>';

        const data = await api('/video-station/recent?limit=20');
        if (data.error) { content.innerHTML = '<div class="vs-empty">' + data.error + '</div>'; return; }

        const items = data.items || [];
        if (!items.length) {
            content.innerHTML = '<div class="vs-empty"><i class="fas fa-clock"></i><p>' + t('Brak ostatnich filmów') + '</p></div>';
            return;
        }
        content.innerHTML = renderGrid(items);
        attachGridEvents(content);
    }

    /* ── collections ───────────────────────────────────────── */
    async function loadCollections() {
        const content = bodyEl.querySelector('#vs-content');
        if (!content) return;
        content.innerHTML = '<div class="vs-loading"><i class="fas fa-spinner fa-spin"></i></div>';

        const data = await api('/video-station/collections');
        if (data.error) { content.innerHTML = '<div class="vs-empty">' + data.error + '</div>'; return; }

        const cols = data.collections || [];
        if (!cols.length) {
            content.innerHTML = '<div class="vs-empty"><i class="fas fa-folder-open"></i><p>' + t('Brak kolekcji') + '</p></div>';
            return;
        }

        let html = '<div class="vs-grid">';
        cols.forEach(c => {
            const thumbHtml = c.cover_id
                ? '<img src="/api/video-station/thumb/' + c.cover_id + '?token=' + NAS.token + '" loading="lazy" alt="">'
                : '<div class="vs-thumb-placeholder"><i class="fas fa-folder-open"></i></div>';
            html +=
                '<div class="vs-card vs-collection-card" data-folder="' + escH(c.path || c.folder || '') + '">' +
                  '<div class="vs-thumb vs-collection-thumb">' + thumbHtml +
                    '<span class="vs-collection-count">' + (c.count || 0) + ' ' + t('filmów') + '</span>' +
                  '</div>' +
                  '<div class="vs-title">' + escH(c.name || c.folder || '') + '</div>' +
                '</div>';
        });
        html += '</div>';
        content.innerHTML = html;

        content.querySelectorAll('.vs-collection-card').forEach(card => {
            card.onclick = () => {
                currentFolder = card.dataset.folder;
                switchSection('library');
            };
        });
    }

    /* ── folders settings ──────────────────────────────────── */
    async function loadFolders() {
        const content = bodyEl.querySelector('#vs-content');
        if (!content) return;
        content.innerHTML = '<div class="vs-loading"><i class="fas fa-spinner fa-spin"></i></div>';

        const data = await api('/video-station/folders');
        if (data.error) { content.innerHTML = '<div class="vs-empty">' + data.error + '</div>'; return; }

        const folders = data.folders || [];
        let listHtml = '';
        if (folders.length) {
            folders.forEach(f => {
                listHtml +=
                    '<div class="vs-folder-row">' +
                      '<i class="fas fa-folder"></i>' +
                      '<span class="vs-folder-path">' + escH(f) + '</span>' +
                      '<button class="app-btn app-btn-sm vs-folder-remove" data-path="' + escH(f) + '" title="' + t('Usuń') + '"><i class="fas fa-trash"></i></button>' +
                    '</div>';
            });
        } else {
            listHtml = '<div class="vs-empty-small">' + t('Brak folderów. Dodaj folder aby rozpocząć.') + '</div>';
        }

        content.innerHTML =
'<div class="vs-folders-panel">' +
  '<div class="vs-folders-header">' +
    '<span>' + t('Foldery monitorowane') + '</span>' +
    '<button id="vs-add-folder" class="app-btn app-btn-sm app-btn-primary"><i class="fas fa-plus"></i> ' + t('Dodaj') + '</button>' +
  '</div>' +
  '<div class="vs-folders-list" id="vs-folders-list">' + listHtml + '</div>' +
  '<div class="vs-settings-section">' +
    '<div class="vs-folders-header"><span><i class="fas fa-magic"></i> TMDb — ' + t('rozpoznawanie filmów') + '</span></div>' +
    '<p class="vs-settings-desc">' + t('Podaj klucz API z') + ' <a href="https://www.themoviedb.org/settings/api" target="_blank" style="color:var(--accent)">themoviedb.org</a> ' + t('aby automatycznie pobierać okładki, opisy i oceny filmów.') + '</p>' +
    '<div class="vs-tmdb-key-row">' +
      '<input type="text" id="vs-tmdb-key" class="vs-input" placeholder="' + t('Klucz API TMDb (v3)') + '">' +
      '<button id="vs-tmdb-save" class="app-btn app-btn-sm app-btn-primary">' + t('Zapisz') + '</button>' +
      '<span id="vs-tmdb-status" class="vs-tmdb-status"></span>' +
    '</div>' +
  '</div>' +
  '<div class="vs-folders-footer">' +
    '<button id="vs-uninstall-btn" class="app-btn app-btn-sm" style="color:var(--danger)"><i class="fas fa-trash-alt"></i> ' + t('Odinstaluj Video Station') + '</button>' +
  '</div>' +
'</div>';

        content.querySelector('#vs-add-folder').onclick = () => {
            openDirPicker('/home', t('Wybierz folder z filmami'), async (path) => {
                if (!path) return;
                const cur = (await api('/video-station/folders')).folders || [];
                if (cur.includes(path)) { toast(t('Folder już dodany'), 'warning'); return; }
                cur.push(path);
                const res = await api('/video-station/folders', { method: 'POST', body: { folders: cur } });
                if (res.error) { toast(res.error, 'error'); return; }
                toast(t('Folder dodany'), 'success');
                loadFolders();
            });
        };

        content.querySelectorAll('.vs-folder-remove').forEach(btn => {
            btn.onclick = async () => {
                const path = btn.dataset.path;
                if (!await confirmDialog(t('Czy na pewno usunąć folder') + ' ' + path + '?')) return;
                const cur = (await api('/video-station/folders')).folders || [];
                const next = cur.filter(f => f !== path);
                const res = await api('/video-station/folders', { method: 'POST', body: { folders: next } });
                if (res.error) { toast(res.error, 'error'); return; }
                toast(t('Folder usunięty'), 'success');
                loadFolders();
            };
        });

        content.querySelector('#vs-uninstall-btn').onclick = async () => {
            if (!await confirmDialog(t('Czy na pewno odinstalować Video Station? Metadane biblioteki zostaną usunięte.'))) return;
            const res = await api('/video-station/uninstall', { method: 'POST' });
            if (res.error) { toast(res.error, 'error'); return; }
            toast(t('Odinstalowano'), 'success');
            init(bodyEl);
        };

        // TMDb API key config
        const tmdbConf = await api('/video-station/tmdb-config');
        const tmdbStatus = content.querySelector('#vs-tmdb-status');
        if (tmdbConf && tmdbConf.has_key) {
            tmdbStatus.innerHTML = '<i class="fas fa-check-circle" style="color:var(--success)"></i> ' + t('Klucz aktywny') + ' (' + tmdbConf.key_preview + ')';
        }
        content.querySelector('#vs-tmdb-save').onclick = async () => {
            const key = content.querySelector('#vs-tmdb-key').value.trim();
            if (!key) { toast(t('Podaj klucz API'), 'warning'); return; }
            const res = await api('/video-station/tmdb-config', { method: 'POST', body: { api_key: key } });
            if (res.error) { toast(res.error, 'error'); return; }
            toast(t('Klucz TMDb zapisany!'), 'success');
            content.querySelector('#vs-tmdb-key').value = '';
            tmdbStatus.innerHTML = '<i class="fas fa-check-circle" style="color:var(--success)"></i> ' + t('Klucz aktywny');
        };
    }

    /* ── scan ──────────────────────────────────────────────── */
    async function startScan() {
        const res = await api('/video-station/scan', { method: 'POST', body: { use_tmdb: useTmdb } });
        if (res.error) { toast(res.error, 'error'); return; }
        scanning = true;
        updateScanUI({ running: true, total: 0, processed: 0, current_file: '' });
    }

    async function matchAll() {
        const res = await api('/video-station/tmdb-match-all', { method: 'POST' });
        if (res.error) { toast(res.error, 'error'); return; }
        scanning = true;
        toast(t('Dopasowywanie filmów do TMDb...'), 'info');
        updateScanUI({ running: true, total: 0, processed: 0, current_file: '' });
    }

    async function stopScan() {
        await api('/video-station/scan-stop', { method: 'POST' });
        scanning = false;
        updateScanUI({ running: false });
    }

    async function checkScanStatus() {
        const d = await api('/video-station/scan-status');
        if (d && d.running) { scanning = true; updateScanUI(d); }
    }

    function updateScanUI(d) {
        const btn  = bodyEl && bodyEl.querySelector('#vs-scan-btn');
        const bar  = bodyEl && bodyEl.querySelector('#vs-scan-bar');
        const fill = bodyEl && bodyEl.querySelector('#vs-scan-fill');
        const text = bodyEl && bodyEl.querySelector('#vs-scan-text');
        if (!btn || !bar) return;

        if (d.running) {
            btn.style.display = 'none';
            bar.style.display = 'flex';
            const pct = d.total ? Math.round((d.processed / d.total) * 100) : 0;
            if (fill) fill.style.width = pct + '%';
            if (text) text.textContent = d.processed + '/' + d.total;
        } else {
            btn.style.display = '';
            bar.style.display = 'none';
        }
    }

    /* ── grid rendering ────────────────────────────────────── */
    function renderGrid(items) {
        let html = '<div class="vs-grid">';
        items.forEach(v => {
            const dur = formatDuration(v.duration);
            const pct = v.duration && v.position ? Math.min(100, Math.round((v.position / v.duration) * 100)) : 0;
            const res = v.height ? (v.height >= 2160 ? '4K' : v.height >= 1080 ? '1080p' : v.height >= 720 ? '720p' : v.height + 'p') : '';
            const codec = v.codec || '';
            const size = v.file_size ? formatBytes(v.file_size) : '';
            const meta = [res, codec, size].filter(Boolean).join(' \u00b7 ');
            const hasPoster = v.poster_ok;
            const imgSrc = hasPoster
                ? '/api/video-station/poster/' + v.id + '?token=' + NAS.token
                : '/api/video-station/thumb/' + v.id + '?token=' + NAS.token;
            const rating = v.tmdb_rating ? v.tmdb_rating.toFixed(1) : '';

            html +=
'<div class="vs-card' + (hasPoster ? ' vs-card-poster' : '') + '" data-id="' + v.id + '">' +
  '<div class="vs-thumb' + (hasPoster ? ' vs-thumb-poster' : '') + '">' +
    '<img src="' + imgSrc + '" loading="lazy" alt="" onerror="this.style.display=\'none\'">' +
    '<div class="vs-thumb-placeholder"><i class="fas fa-film"></i></div>' +
    (dur ? '<span class="vs-duration">' + dur + '</span>' : '') +
    (rating ? '<span class="vs-rating"><i class="fas fa-star"></i> ' + rating + '</span>' : '') +
    (pct > 0 && !v.watched ? '<div class="vs-progress" style="width:' + pct + '%"></div>' : '') +
    (v.watched ? '<span class="vs-watched"><i class="fas fa-check-circle"></i></span>' : '') +
  '</div>' +
  '<div class="vs-title" title="' + escH(v.title || v.filename || '') + '">' + escH(v.title || v.filename || '') + '</div>' +
  (meta ? '<div class="vs-meta">' + escH(meta) + '</div>' : '') +
'</div>';
        });
        html += '</div>';
        return html;
    }

    function attachGridEvents(container) {
        container.querySelectorAll('.vs-card[data-id]').forEach(card => {
            card.onclick = () => openPlayer(card.dataset.id);
        });
    }

    /* ── pagination ────────────────────────────────────────── */
    function renderPagination() {
        if (libraryTotal <= PAGE_SIZE) return '';
        const totalPages = Math.ceil(libraryTotal / PAGE_SIZE);
        const currentPage = Math.floor(libraryOffset / PAGE_SIZE) + 1;
        return '<div class="vs-pagination">' +
            '<button class="app-btn app-btn-sm vs-page-prev"' + (currentPage <= 1 ? ' disabled' : '') + '><i class="fas fa-chevron-left"></i></button>' +
            '<span class="vs-page-info">' + currentPage + ' / ' + totalPages + '</span>' +
            '<button class="app-btn app-btn-sm vs-page-next"' + (currentPage >= totalPages ? ' disabled' : '') + '><i class="fas fa-chevron-right"></i></button>' +
            '</div>';
    }

    function attachPaginationEvents(container) {
        const prev = container.querySelector('.vs-page-prev');
        const next = container.querySelector('.vs-page-next');
        if (prev) prev.onclick = () => { libraryOffset = Math.max(0, libraryOffset - PAGE_SIZE); loadLibrary(); };
        if (next) next.onclick = () => { libraryOffset += PAGE_SIZE; loadLibrary(); };
    }

    /* ── video player ──────────────────────────────────────── */
    async function openPlayer(vid) {
        const info = await api('/video-station/info/' + vid);
        if (info.error) { toast(info.error, 'error'); return; }

        const overlay = bodyEl.querySelector('#vs-player-overlay');
        const video   = bodyEl.querySelector('#vs-player-video');
        const title   = bodyEl.querySelector('#vs-player-title');
        if (!overlay || !video) return;

        title.textContent = info.title || info.filename || '';
        video.src = '/api/video-station/stream/' + vid + '?token=' + NAS.token;

        // resume from last position
        if (info.position && info.position > 0 && !info.watched) {
            video.addEventListener('loadedmetadata', function onMeta() {
                video.currentTime = info.position;
                video.removeEventListener('loadedmetadata', onMeta);
            });
        }

        overlay.style.display = 'flex';
        video.focus();

        // save position every 10 seconds
        playerInterval = setInterval(() => savePosition(vid, video), 10000);

        // mark watched at >90%
        video.ontimeupdate = () => {
            if (video.duration && video.currentTime / video.duration > 0.9) {
                api('/video-station/watched/' + vid, { method: 'POST', body: { watched: true, position: video.currentTime } });
                video.ontimeupdate = null;
            }
        };

        video.onended = () => {
            api('/video-station/watched/' + vid, { method: 'POST', body: { watched: true, position: video.duration } });
            closePlayer();
        };

        // keyboard
        overlay._keyHandler = (e) => {
            if (e.key === ' ' || e.code === 'Space') { e.preventDefault(); video.paused ? video.play() : video.pause(); }
            else if (e.key === 'f') { toggleFullscreen(video); }
            else if (e.key === 'Escape') { closePlayer(); }
            else if (e.key === 'ArrowLeft') { video.currentTime = Math.max(0, video.currentTime - 10); }
            else if (e.key === 'ArrowRight') { video.currentTime = Math.min(video.duration || 0, video.currentTime + 10); }
        };
        document.addEventListener('keydown', overlay._keyHandler);

        bodyEl.querySelector('#vs-player-close').onclick = () => closePlayer();
    }

    function savePosition(vid, video) {
        if (!video || video.paused) return;
        api('/video-station/watched/' + vid, { method: 'POST', body: { watched: false, position: video.currentTime } });
    }

    function closePlayer() {
        const overlay = bodyEl.querySelector('#vs-player-overlay');
        const video   = bodyEl.querySelector('#vs-player-video');
        if (!overlay) return;

        stopPlayer();
        overlay.style.display = 'none';
        if (video) { video.pause(); video.removeAttribute('src'); video.load(); }
        if (overlay._keyHandler) { document.removeEventListener('keydown', overlay._keyHandler); overlay._keyHandler = null; }

        // refresh current view to reflect watch state
        if (activeSection === 'library') loadLibrary();
        else if (activeSection === 'recent') loadRecent();
    }

    function stopPlayer() {
        if (playerInterval) { clearInterval(playerInterval); playerInterval = null; }
    }

    function toggleFullscreen(el) {
        if (!document.fullscreenElement) el.requestFullscreen().catch(() => {});
        else document.exitFullscreen();
    }

    /* ── helpers ────────────────────────────────────────────── */
    function formatDuration(sec) {
        if (!sec || sec <= 0) return '';
        sec = Math.round(sec);
        const h = Math.floor(sec / 3600);
        const m = Math.floor((sec % 3600) / 60);
        const s = sec % 60;
        if (h > 0) return h + ':' + String(m).padStart(2, '0') + ':' + String(s).padStart(2, '0');
        return m + ':' + String(s).padStart(2, '0');
    }

    function escH(str) {
        if (!str) return '';
        return String(str).replace(/&/g, '&amp;').replace(/</g, '&lt;').replace(/>/g, '&gt;').replace(/"/g, '&quot;');
    }

    /* ── CSS ───────────────────────────────────────────────── */
    function getCSS() { return [
/* install screen */
'.vs-install-wrap{display:flex;align-items:center;justify-content:center;height:100%;padding:32px}',
'.vs-install-card{text-align:center;max-width:440px}',
'.vs-install-icon{font-size:56px;color:var(--accent);margin-bottom:16px}',
'.vs-install-card h2{margin:0 0 8px;color:var(--text-primary)}',
'.vs-install-sub{color:var(--text-muted);font-size:13px;margin:0 0 16px}',
'.vs-install-deps{font-size:12px;color:var(--text-muted);background:var(--bg-secondary);border-radius:var(--r-md);padding:8px 14px;margin-bottom:16px;font-family:monospace}',

/* progress bar (shared) */
'.vs-prog-bar{height:6px;background:var(--bg-secondary);border-radius:3px;overflow:hidden;width:100%}',
'.vs-prog-fill{height:100%;background:var(--accent);border-radius:3px;transition:width .3s;width:0}',
'.vs-prog-msg{font-size:11px;color:var(--text-muted);margin-top:4px;text-align:center}',

/* main layout */
'.vs-layout{display:flex;height:100%;overflow:hidden}',
'.vs-sidebar{width:200px;min-width:200px;background:var(--bg-secondary);border-right:1px solid var(--border);display:flex;flex-direction:column;overflow-y:auto}',
'.vs-main{flex:1;display:flex;flex-direction:column;overflow:hidden;min-width:0}',

/* sidebar nav */
'.vs-nav-section{font-size:11px;font-weight:600;text-transform:uppercase;color:var(--text-muted);padding:14px 16px 6px;letter-spacing:.04em}',
'.vs-nav-item{display:flex;align-items:center;gap:10px;padding:9px 16px;cursor:pointer;color:var(--text-secondary);border-left:3px solid transparent;font-size:13px;transition:background .15s}',
'.vs-nav-item:hover{background:var(--overlay-1)}',
'.vs-nav-item.active{background:var(--overlay-1);border-left-color:var(--accent);color:var(--accent);font-weight:600}',
'.vs-sidebar-stats{margin-top:auto;padding:12px 16px;border-top:1px solid var(--border);font-size:12px}',
'.vs-stat-row{display:flex;justify-content:space-between;padding:3px 0;color:var(--text-muted)}',

/* toolbar */
'.vs-toolbar{display:flex;align-items:center;justify-content:space-between;gap:10px;padding:10px 16px;border-bottom:1px solid var(--border);flex-wrap:wrap}',
'.vs-toolbar-group{display:flex;align-items:center;gap:8px}',
'.vs-toolbar-title{font-size:15px;font-weight:600;color:var(--text-primary)}',
'.vs-search-box{display:flex;align-items:center;gap:6px;background:var(--bg-deep,var(--bg-secondary));border:1px solid var(--border);border-radius:var(--r-md);padding:0 10px;height:32px}',
'.vs-search-box i{color:var(--text-muted);font-size:12px}',
'.vs-search-box input{border:none;background:transparent;color:var(--text-primary);outline:none;font-size:13px;width:180px}',
'.vs-select{background:var(--bg-deep,var(--bg-secondary));border:1px solid var(--border);color:var(--text-primary);border-radius:var(--r-md);padding:5px 10px;font-size:13px;cursor:pointer}',

/* scan */
'.vs-scan-wrap{display:flex;align-items:center;gap:8px}',
'.vs-scan-progress{display:flex;align-items:center;gap:8px;min-width:200px}',
'.vs-scan-text{font-size:12px;color:var(--text-muted);white-space:nowrap}',

/* content area */
'.vs-content{flex:1;overflow-y:auto;padding:16px}',
'.vs-loading{display:flex;align-items:center;justify-content:center;height:200px;color:var(--text-muted);font-size:20px}',
'.vs-empty{display:flex;flex-direction:column;align-items:center;justify-content:center;height:300px;color:var(--text-muted);gap:8px;font-size:14px}',
'.vs-empty i{font-size:48px;opacity:.4}',
'.vs-empty-sub{font-size:12px;opacity:.7}',
'.vs-empty-small{padding:20px;text-align:center;color:var(--text-muted);font-size:13px}',

/* grid */
'.vs-grid{display:grid;grid-template-columns:repeat(auto-fill,minmax(200px,1fr));gap:16px}',

/* card */
'.vs-card{cursor:pointer;border-radius:var(--r-md);overflow:hidden;transition:transform .15s,box-shadow .15s;background:var(--bg-secondary);border:1px solid var(--border)}',
'.vs-card:hover{transform:translateY(-2px);box-shadow:0 4px 16px rgba(0,0,0,.25);border-color:rgba(79,140,255,.3)}',
'.vs-thumb{position:relative;aspect-ratio:16/9;background:#111;overflow:hidden;display:flex;align-items:center;justify-content:center}',
'.vs-thumb img{width:100%;height:100%;object-fit:cover;position:absolute;inset:0;z-index:1}',
'.vs-thumb-placeholder{color:var(--text-muted);font-size:28px;opacity:.3}',
'.vs-duration{position:absolute;bottom:6px;right:6px;background:rgba(0,0,0,.8);color:#fff;font-size:11px;padding:2px 6px;border-radius:3px;z-index:2;font-variant-numeric:tabular-nums}',
'.vs-rating{position:absolute;top:6px;left:6px;background:rgba(0,0,0,.8);color:#fbbf24;font-size:11px;padding:2px 6px;border-radius:3px;z-index:2;font-weight:600}',
'.vs-rating .fa-star{font-size:10px;margin-right:2px}',
'.vs-progress{position:absolute;bottom:0;left:0;height:3px;background:var(--accent);z-index:3;transition:width .3s}',
'.vs-watched{position:absolute;top:6px;right:6px;color:var(--accent-green);font-size:16px;z-index:2;text-shadow:0 1px 3px rgba(0,0,0,.6)}',

/* poster mode — taller card for movie posters */
'.vs-card-poster .vs-thumb{aspect-ratio:2/3}',
'.vs-card-poster .vs-thumb img{object-fit:cover}',
'.vs-title{padding:8px 10px 2px;font-size:13px;font-weight:500;color:var(--text-primary);white-space:nowrap;overflow:hidden;text-overflow:ellipsis}',
'.vs-meta{padding:0 10px 8px;font-size:11px;color:var(--text-muted);white-space:nowrap;overflow:hidden;text-overflow:ellipsis}',

/* collection card tweaks */
'.vs-collection-thumb{aspect-ratio:16/10}',
'.vs-collection-count{position:absolute;bottom:6px;left:6px;background:rgba(0,0,0,.75);color:#fff;font-size:11px;padding:2px 8px;border-radius:3px;z-index:2}',

/* pagination */
'.vs-pagination{display:flex;align-items:center;justify-content:center;gap:12px;padding:16px 0}',
'.vs-page-info{font-size:13px;color:var(--text-muted)}',

/* folders settings */
'.vs-folders-panel{max-width:600px;margin:0 auto}',
'.vs-folders-header{display:flex;align-items:center;justify-content:space-between;margin-bottom:12px;font-size:14px;font-weight:600;color:var(--text-primary)}',
'.vs-folders-list{border:1px solid var(--border);border-radius:var(--r-md);overflow:hidden}',
'.vs-folder-row{display:flex;align-items:center;gap:10px;padding:10px 14px;border-bottom:1px solid var(--border);font-size:13px;color:var(--text-primary)}',
'.vs-folder-row:last-child{border-bottom:none}',
'.vs-folder-row i.fa-folder{color:var(--accent);font-size:14px}',
'.vs-folder-path{flex:1;overflow:hidden;text-overflow:ellipsis;white-space:nowrap}',
'.vs-folder-remove{opacity:.5;transition:opacity .15s}',
'.vs-folder-remove:hover{opacity:1;color:var(--danger)!important}',
'.vs-folders-footer{margin-top:24px;padding-top:16px;border-top:1px solid var(--border);display:flex;justify-content:flex-end}',

/* tmdb settings */
'.vs-settings-section{margin-top:24px;padding-top:16px;border-top:1px solid var(--border)}',
'.vs-settings-desc{font-size:12px;color:var(--text-muted);margin:4px 0 12px;line-height:1.5}',
'.vs-tmdb-key-row{display:flex;align-items:center;gap:8px}',
'.vs-tmdb-key-row .vs-input{flex:1;padding:6px 10px;border:1px solid var(--border);border-radius:var(--r-sm);background:var(--bg-secondary);color:var(--text-primary);font-size:13px}',
'.vs-tmdb-status{font-size:12px;color:var(--text-muted);white-space:nowrap}',

/* tmdb toolbar checkbox */
'.vs-tmdb-check{display:flex;align-items:center;gap:4px;font-size:12px;color:var(--text-muted);cursor:pointer;white-space:nowrap;user-select:none}',
'.vs-tmdb-check input{margin:0;cursor:pointer}',
'.vs-tmdb-check .fa-magic{font-size:11px;color:#fbbf24}',

/* player overlay */
'.vs-player-overlay{position:absolute;inset:0;background:rgba(0,0,0,.95);z-index:100;display:flex;flex-direction:column;align-items:center;justify-content:center;overflow:hidden}',
'.vs-player-top{position:absolute;top:0;left:0;right:0;display:flex;align-items:center;justify-content:space-between;padding:12px 16px;z-index:101;background:linear-gradient(to bottom,rgba(0,0,0,.7),transparent)}',
'.vs-player-title{color:#fff;font-size:14px;font-weight:500;overflow:hidden;text-overflow:ellipsis;white-space:nowrap}',
'.vs-player-close{background:none;border:none;color:#fff;font-size:20px;cursor:pointer;padding:4px 8px;opacity:.7;transition:opacity .15s}',
'.vs-player-close:hover{opacity:1}',
'#vs-player-video{max-width:100%;max-height:calc(100% - 48px);margin-top:24px;outline:none;border-radius:4px}',
    ].join('\n'); }
};
