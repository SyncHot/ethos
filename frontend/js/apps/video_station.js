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

    const _cl = (level, msg, details) => typeof NAS !== 'undefined' && NAS.logClient
        ? NAS.logClient('video-station', level, msg, details) : console.log('[video-station]', msg, details || '');

    /* ── state ─────────────────────────────────────────────── */
    let activeSection  = 'home';
    let sidebarCollapsed = localStorage.getItem('vs_sidebar_collapsed') === '1';
    let libraryItems   = [];
    let libraryTotal   = 0;
    let libraryOffset  = 0;
    const PAGE_SIZE    = 60;
    let currentSort    = 'added_desc';
    let currentQuery   = '';
    let currentFolder  = '';
    let currentWatched = '';
    let scanning       = false;
    let useTmdb        = true;
    let playerInterval = null;
    let bodyEl         = null;

    /* multi-select */
    let selectMode     = false;
    let selectedIds    = new Set();

    /* hidden-videos state (refreshed from pkg-status) */
    let hidePasswordSet = false;
    let hideUnlocked    = false;
    let hiddenCount     = 0;

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
        if (st.error) { body.innerHTML = '<div style="padding:32px;color:var(--danger)">' + escH(st.error) + '</div>'; return; }
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
        hidePasswordSet = !!st.hide_password_set;
        hideUnlocked = !!st.hide_unlocked;
        hiddenCount = (st.stats && st.stats.hidden) || 0;

        body.innerHTML = `
<style>${getCSS()}</style>
<div class="vs-layout${sidebarCollapsed ? ' vs-sidebar-collapsed' : ''}">
  <div class="vs-sidebar">
    <div class="vs-sidebar-header">
      <span class="vs-sidebar-logo"><i class="fas fa-film"></i><span> Video Station</span></span>
      <button class="vs-hamburger" id="vs-hamburger" title="${t('Zwiń/rozwiń')}"><i class="fas fa-bars"></i></button>
    </div>
    <div class="vs-nav-section vs-nav-label">${t('Odkrywaj')}</div>
    <div class="vs-nav-item active" data-section="home"><i class="fas fa-home"></i><span>${t('Strona główna')}</span></div>
    <div class="vs-nav-label vs-nav-section">${t('Biblioteka')}</div>
    <div class="vs-nav-item" data-section="library"><i class="fas fa-film"></i><span>${t('Wszystkie filmy')}</span></div>
    <div class="vs-nav-item" data-section="recent"><i class="fas fa-clock"></i><span>${t('Ostatnie')}</span></div>
    <div class="vs-nav-item" data-section="history"><i class="fas fa-history"></i><span>${t('Historia')}</span></div>
    <div class="vs-nav-item" data-section="collections"><i class="fas fa-folder-open"></i><span>${t('Kolekcje')}</span></div>
    <div class="vs-nav-item" data-section="hidden"><i class="fas fa-eye-slash"></i><span>${t('Ukryte')}</span>${hiddenCount ? '<span class="vs-nav-badge">' + hiddenCount + '</span>' : ''}</div>
    <div class="vs-nav-label vs-nav-section">${t('Zarządzanie')}</div>
    <div class="vs-nav-item" data-section="settings"><i class="fas fa-sliders-h"></i><span>${t('Ustawienia')}</span></div>
    <div class="vs-nav-item" data-section="folders"><i class="fas fa-folder-open"></i><span>${t('Foldery')}</span></div>
    <div class="vs-sidebar-stats" id="vs-sidebar-stats"></div>
  </div>
  <div class="vs-main">
    <div class="vs-toolbar" id="vs-toolbar"></div>
    <div class="vs-batch-bar" id="vs-batch-bar" style="display:none"></div>
    <div class="vs-content" id="vs-content"></div>
  </div>
</div>
<div class="vs-player-overlay" id="vs-player-overlay" style="display:none">
  <div class="vs-player-top">
    <span class="vs-player-title" id="vs-player-title"></span>
    <span class="vs-player-badge" id="vs-player-badge" style="display:none"><i class="fas fa-sync-alt fa-spin"></i> ${t('Transkodowanie')}</span>
    <select class="vs-audio-select" id="vs-audio-select" style="display:none"></select>
    <select class="vs-sub-select" id="vs-sub-select" style="display:none"></select>
    <select class="vs-speed-select" id="vs-speed-select">
      <option value="0.5">0.5x</option>
      <option value="0.75">0.75x</option>
      <option value="1" selected>1x</option>
      <option value="1.25">1.25x</option>
      <option value="1.5">1.5x</option>
      <option value="2">2x</option>
    </select>
    <button class="vs-pip-btn" id="vs-pip-btn" title="${t('Obraz w obrazie')}"><i class="fas fa-external-link-alt"></i></button>
    <button class="vs-cast-btn" id="vs-cast-btn" title="${t('Cast na TV')}" style="display:none"><i class="fas fa-tv"></i></button>
    <button class="vs-stats-btn" id="vs-stats-btn" title="${t('Statystyki dla geeków')}"><i class="fas fa-chart-bar"></i></button>
    <button class="vs-fs-btn" id="vs-fs-btn" title="${t('Pełny ekran')}"><i class="fas fa-expand"></i></button>
    <button class="vs-player-close" id="vs-player-close"><i class="fas fa-times"></i></button>
  </div>
  <video id="vs-player-video" autoplay playsinline></video>
  <div class="vs-player-center" id="vs-player-center">
    <button class="vs-cc-btn" id="vs-cc-rw"><i class="fas fa-undo-alt"></i><span>10</span></button>
    <button class="vs-cc-btn vs-cc-play" id="vs-cc-play"><i class="fas fa-pause"></i></button>
    <button class="vs-cc-btn" id="vs-cc-ff"><i class="fas fa-redo-alt"></i><span>10</span></button>
  </div>
  <div class="vs-player-bottom" id="vs-player-bottom">
    <span class="vs-pb-time" id="vs-pb-cur">0:00</span>
    <div class="vs-pb-seek-wrap" id="vs-pb-seek-wrap">
      <div class="vs-pb-seek-track">
        <div class="vs-pb-seek-buf" id="vs-pb-seek-buf"></div>
        <div class="vs-pb-seek-fill" id="vs-pb-seek-fill"></div>
      </div>
      <input type="range" class="vs-pb-seek" id="vs-pb-seek" min="0" max="100" value="0" step="0.1">
    </div>
    <span class="vs-pb-time" id="vs-pb-dur">0:00</span>
  </div>
  <div class="vs-seek-hint vs-seek-hint-left" id="vs-seek-hint-left"><i class="fas fa-backward"></i><span>-10s</span></div>
  <div class="vs-seek-hint vs-seek-hint-right" id="vs-seek-hint-right"><i class="fas fa-forward"></i><span>+10s</span></div>
  <div class="vs-swipe-hint" id="vs-swipe-hint"></div>
  <div class="vs-stats-overlay" id="vs-stats-overlay" style="display:none"></div>
  <div class="vs-thumbstrip-preview" id="vs-thumbstrip-preview" style="display:none">
    <canvas id="vs-thumbstrip-canvas" width="160" height="90"></canvas>
    <span class="vs-thumbstrip-time" id="vs-thumbstrip-time"></span>
  </div>
  <div class="vs-resume-dialog" id="vs-resume-dialog" style="display:none">
    <div class="vs-resume-box">
      <div class="vs-resume-text" id="vs-resume-text"></div>
      <div class="vs-resume-btns">
        <button class="vs-resume-btn" id="vs-resume-continue"><i class="fas fa-play"></i> ${t('Kontynuuj')}</button>
        <button class="vs-resume-btn vs-resume-secondary" id="vs-resume-restart"><i class="fas fa-redo"></i> ${t('Od początku')}</button>
      </div>
    </div>
  </div>
</div>
<div class="vs-info-modal" id="vs-info-modal" style="display:none">
  <div class="vs-info-content" id="vs-info-content"></div>
</div>`;

        if (st.stats) updateSidebarStats(body, st.stats);

        body.querySelectorAll('.vs-nav-item').forEach(n => {
            n.onclick = () => switchSection(n.dataset.section);
        });

        // Hamburger toggle
        const hamburger = body.querySelector('#vs-hamburger');
        if (hamburger) {
            hamburger.onclick = () => {
                sidebarCollapsed = !sidebarCollapsed;
                localStorage.setItem('vs_sidebar_collapsed', sidebarCollapsed ? '1' : '0');
                body.querySelector('.vs-layout').classList.toggle('vs-sidebar-collapsed', sidebarCollapsed);
            };
        }

        switchSection('home');

        // Async HW health check — show banner if drivers missing
        _checkHwHealth(body);
    }

    /* ── HW health banner & setup wizard ──────────────────── */
    async function _checkHwHealth(body) {
        let health;
        try { health = await api('/video-station/hw-health'); } catch(e) { return; }
        if (!health || health.is_hw || health.status === 'cpu_only') return;

        // Inject banner into vs-main (above toolbar)
        const vsMain = body.querySelector('.vs-main');
        if (!vsMain || vsMain.querySelector('.vs-hw-banner')) return;

        const isError = health.status === 'permission_denied' || health.status === 'missing_driver';
        const banner = document.createElement('div');
        banner.className = 'vs-hw-banner' + (isError ? ' vs-hw-banner-error' : '');
        banner.innerHTML =
            '<i class="fas fa-' + (isError ? 'lock' : 'exclamation-triangle') + '"></i>' +
            '<span class="vs-hw-banner-msg">' +
              escH(health.message || t('Akceleracja sprzętowa niedostępna.')) +
            '</span>' +
            '<button class="vs-hw-banner-btn"><i class="fas fa-tools"></i> ' + t('Napraw') + '</button>' +
            '<button class="vs-hw-banner-close" title="' + t('Zamknij') + '"><i class="fas fa-times"></i></button>';

        const toolbar = vsMain.querySelector('#vs-toolbar');
        vsMain.insertBefore(banner, toolbar);

        banner.querySelector('.vs-hw-banner-btn').onclick = () => _openHwWizard(body, health);
        banner.querySelector('.vs-hw-banner-close').onclick = () => banner.remove();
    }

    function _openHwWizard(body, health) {
        // Remove existing modal if any
        const existing = body.querySelector('.vs-hw-modal');
        if (existing) existing.remove();

        const stepsHtml = (health.setup_steps || []).map(step => {
            const cmds = (step.commands || []).map(cmd =>
                '<div class="vs-hw-cmd">' +
                  '<code>' + escH(cmd) + '</code>' +
                  '<button class="vs-hw-cmd-copy" data-cmd="' + escH(cmd) + '" title="' + t('Kopiuj') + '">' +
                    '<i class="fas fa-copy"></i>' +
                  '</button>' +
                '</div>'
            ).join('');
            return '<div class="vs-hw-step"><div class="vs-hw-step-title">' + escH(step.title) + '</div>' + cmds + '</div>';
        }).join('');

        const hwIcon = health.is_intel ? 'fa-microchip' : health.is_nvidia ? 'fa-bolt' : 'fa-server';
        const cpuLabel = health.cpu_model ? '<span class="vs-hw-cpu-label">' + escH(health.cpu_model) + '</span>' : '';
        const grpLabel = health.render_grp ? escH(t('Dostęp do grupy') + ' "' + health.render_grp + '"') : t('Dostęp do grupy render');
        const dockerWarning = health.in_docker
            ? '<div class="vs-hw-docker-warn"><i class="fab fa-docker"></i> ' +
              t('Wykryto środowisko Docker. Dodaj flagę') +
              ' <code>--device /dev/dri:/dev/dri</code> ' +
              t('do konfiguracji kontenera.') + '</div>'
            : '';

        // iHD-specific alert banner (shown prominently for IHD_INIT_FAILED)
        const ihdAlert = health.status === 'ihd_init_failed'
            ? '<div class="vs-hw-ihd-alert">' +
              '<div class="vs-hw-ihd-alert-header">' +
                '<i class="fas fa-exclamation-circle"></i>' +
                '<strong>' + t('Wykryto procesor Intel N100, ale sterownik iHD nie może wystartować.') + '</strong>' +
              '</div>' +
              '<div class="vs-hw-ihd-alert-body">' +
                '<div class="vs-hw-ihd-row">' +
                  '<span class="vs-hw-ihd-label">iHD_drv_video.so</span>' +
                  '<span class="vs-hw-ihd-val' + (health.iHD_present ? ' ok' : ' fail') + '">' +
                    (health.iHD_present ? t('Znaleziony') + ' — init failed' : t('Brak')) +
                  '</span>' +
                '</div>' +
                '<div class="vs-hw-ihd-row">' +
                  '<span class="vs-hw-ihd-label">LIBVA_DRIVER_NAME</span>' +
                  '<span class="vs-hw-ihd-val' + (health.libva_correct ? ' ok' : ' warn') + '">' +
                    (health.libva_driver_name ? escH(health.libva_driver_name) : t('Nie ustawiono')) +
                    (health.libva_correct ? '' : ' → ' + t('powinno być: ihd')) +
                  '</span>' +
                '</div>' +
              '</div>' +
            '</div>'
            : '';

        const modal = document.createElement('div');
        modal.className = 'vs-hw-modal';
        modal.innerHTML =
          '<div class="vs-hw-modal-backdrop"></div>' +
          '<div class="vs-hw-modal-box">' +
            '<div class="vs-hw-modal-header">' +
              '<i class="fas ' + hwIcon + '"></i> ' +
              '<span>' + t('Konfiguracja akceleracji sprzętowej') + '</span>' +
              '<button class="vs-hw-modal-close"><i class="fas fa-times"></i></button>' +
            '</div>' +
            '<div class="vs-hw-modal-body">' +
              ihdAlert +
              (ihdAlert ? '' :
                '<div class="vs-hw-modal-status vs-hw-status-' + escH(health.status) + '">' +
                  '<i class="fas fa-info-circle"></i> ' + escH(health.message) +
                '</div>'
              ) +
              dockerWarning +
              (cpuLabel ? '<div class="vs-hw-modal-cpu"><i class="fas fa-microchip"></i> ' + cpuLabel +
                (health.process_user ? ' &nbsp;·&nbsp; <i class="fas fa-user"></i> ' + escH(health.process_user) : '') +
              '</div>' : '') +
              '<div class="vs-hw-modal-diag">' +
                _hwDiagRow(t('Węzeł GPU (/dev/dri/renderD128)'), health.render_node) +
                _hwDiagRow(grpLabel, health.in_render_grp) +
                _hwDiagRow(t('Sterownik iHD_drv_video.so'), health.iHD_present) +
                (health.status === 'ihd_init_failed' ? _hwDiagRow('LIBVA_DRIVER_NAME=ihd', health.libva_correct) : '') +
                _hwDiagRow(t('Sterownik VAAPI aktywny'), health.driver_ok) +
                _hwDiagRow(t('Aktywna akceleracja HW'), health.is_hw) +
              '</div>' +
              (stepsHtml ? '<div class="vs-hw-modal-steps">' + stepsHtml + '</div>' : '') +
              '<div id="vs-hw-vainfo-result" class="vs-hw-vainfo-result" style="display:none"></div>' +
            '</div>' +
            '<div class="vs-hw-modal-footer">' +
              (health.status !== 'ok' && health.status !== 'cpu_only' ?
                '<button id="vs-hw-autoinstall-btn" class="app-btn app-btn-primary">' +
                  '<i class="fas fa-magic"></i> ' + t('Zainstaluj automatycznie') +
                '</button>' : '') +
              '<button id="vs-hw-vainfo-btn" class="app-btn">' +
                '<i class="fas fa-vial"></i> ' + t('Testuj ponownie') +
              '</button>' +
              '<button id="vs-hw-rescan-btn" class="app-btn">' +
                '<i class="fas fa-sync-alt"></i> ' + t('Skanuj ponownie') +
              '</button>' +
              '<button class="vs-hw-modal-close-btn app-btn">' + t('Zamknij') + '</button>' +
            '</div>' +
            '<pre id="vs-hw-install-log" class="vs-hw-install-log" style="display:none"></pre>' +
          '</div>';

        body.appendChild(modal);

        // Close handlers
        const closeFn = () => modal.remove();
        modal.querySelector('.vs-hw-modal-close').onclick = closeFn;
        modal.querySelector('.vs-hw-modal-close-btn').onclick = closeFn;
        modal.querySelector('.vs-hw-modal-backdrop').onclick = closeFn;

        // Copy buttons
        modal.querySelectorAll('.vs-hw-cmd-copy').forEach(btn => {
            btn.onclick = () => {
                navigator.clipboard.writeText(btn.dataset.cmd).then(() => {
                    btn.innerHTML = '<i class="fas fa-check"></i>';
                    setTimeout(() => { btn.innerHTML = '<i class="fas fa-copy"></i>'; }, 1500);
                });
            };
        });

        // Auto-install button
        const autoBtn = modal.querySelector('#vs-hw-autoinstall-btn');
        if (autoBtn) {
            autoBtn.onclick = async () => {
                const log = modal.querySelector('#vs-hw-install-log');
                autoBtn.disabled = true;
                autoBtn.innerHTML = '<i class="fas fa-spinner fa-spin"></i> ' + t('Instaluję...');
                log.style.display = 'block';
                log.textContent = '';

                try {
                    const resp = await fetch('/api/video-station/hw-install', {
                        method: 'POST',
                        headers: {
                            'Authorization': 'Bearer ' + NAS.token,
                            'X-CSRF-Token': NAS.csrfToken || '',
                        }
                    });
                    const reader = resp.body.getReader();
                    const decoder = new TextDecoder();
                    let buffer = '';
                    let finalData = null;

                    while (true) {
                        const { done, value } = await reader.read();
                        if (done) break;
                        buffer += decoder.decode(value, { stream: true });
                        const parts = buffer.split('\n\n');
                        buffer = parts.pop();
                        for (const part of parts) {
                            if (!part.startsWith('data: ')) continue;
                            try {
                                const ev = JSON.parse(part.slice(6));
                                if (ev.line) {
                                    log.textContent += ev.line + '\n';
                                    log.scrollTop = log.scrollHeight;
                                }
                                if (ev.done) finalData = ev;
                            } catch(e) {}
                        }
                    }

                    if (finalData && finalData.ok) {
                        if (finalData.restart_required) {
                            toast(t('Sterowniki zainstalowane — wymagany restart serwisu EthOS'), 'warning');
                        } else {
                            toast(t('Akceleracja sprzętowa aktywna!') + ' (' + (finalData.hw_encoder || '') + ')', 'success');
                            modal.remove();
                            body.querySelector('.vs-hw-banner')?.remove();
                            _updateHwBadges(body, true);
                        }
                    } else {
                        toast(t('Instalacja nie powiodła się — sprawdź log poniżej'), 'error');
                        autoBtn.disabled = false;
                        autoBtn.innerHTML = '<i class="fas fa-magic"></i> ' + t('Zainstaluj automatycznie');
                    }
                } catch(e) {
                    toast(t('Błąd połączenia') + ': ' + e.message, 'error');
                    autoBtn.disabled = false;
                    autoBtn.innerHTML = '<i class="fas fa-magic"></i> ' + t('Zainstaluj automatycznie');
                }
            };
        }

        // Testuj ponownie — runs vainfo directly
        modal.querySelector('#vs-hw-vainfo-btn').onclick = async () => {
            const btn = modal.querySelector('#vs-hw-vainfo-btn');
            const resultBox = modal.querySelector('#vs-hw-vainfo-result');
            btn.disabled = true;
            btn.innerHTML = '<i class="fas fa-spinner fa-spin"></i> ' + t('Testuję...');
            resultBox.style.display = 'none';
            try {
                const res = await api('/video-station/vainfo-test');
                if (res.ok) {
                    const profileList = (res.profiles || []).slice(0, 8).map(p =>
                        '<li>' + escH(p) + '</li>'
                    ).join('');
                    resultBox.className = 'vs-hw-vainfo-result vs-hw-vainfo-ok';
                    resultBox.innerHTML =
                        '<i class="fas fa-check-circle"></i> <strong>' + t('vainfo: OK') + '</strong>' +
                        (profileList ? '<ul class="vs-hw-vainfo-profiles">' + profileList + '</ul>' : '');
                    resultBox.style.display = 'block';
                    toast(t('GPU działa prawidłowo — akceleracja Intel QuickSync aktywna'), 'success');
                    body.querySelector('.vs-hw-banner')?.remove();
                    _updateHwBadges(body, true);
                } else {
                    resultBox.className = 'vs-hw-vainfo-result vs-hw-vainfo-fail';
                    resultBox.innerHTML =
                        '<i class="fas fa-times-circle"></i> <strong>' + t('vainfo: błąd') + '</strong>' +
                        (res.error ? '<div class="vs-hw-vainfo-err">' + escH(res.error) + '</div>' : '');
                    resultBox.style.display = 'block';
                }
            } catch(e) {
                toast(t('Błąd testu vainfo') + ': ' + e.message, 'error');
            } finally {
                btn.disabled = false;
                btn.innerHTML = '<i class="fas fa-vial"></i> ' + t('Testuj ponownie');
            }
        };

        // Rescan
        modal.querySelector('#vs-hw-rescan-btn').onclick = async () => {
            const btn = modal.querySelector('#vs-hw-rescan-btn');
            btn.disabled = true;
            btn.innerHTML = '<i class="fas fa-spinner fa-spin"></i> ' + t('Sprawdzam...');
            try {
                const fresh = await api('/video-station/hw-health');
                modal.remove();
                body.querySelector('.vs-hw-banner')?.remove();
                if (fresh && fresh.is_hw) {
                    toast(t('Akceleracja sprzętowa aktywna!') + ' (' + escH(fresh.hw_encoder) + ')', 'success');
                    _updateHwBadges(body, true);
                } else {
                    _checkHwHealth(body);
                    _openHwWizard(body, fresh || health);
                }
            } catch(e) {
                btn.disabled = false;
                btn.innerHTML = '<i class="fas fa-sync-alt"></i> ' + t('Skanuj ponownie');
            }
        };
    }

    /** Update all HW encoder badges in the current app window to reflect active/confirmed state. */
    function _updateHwBadges(body, active) {
        body.querySelectorAll('.vs-hw-badge').forEach(badge => {
            badge.classList.remove('vs-hw-badge-sw', 'vs-hw-badge-hw', 'vs-hw-badge-active');
            badge.classList.add(active ? 'vs-hw-badge-active' : 'vs-hw-badge-sw');
            const icon = badge.querySelector('i');
            if (icon) icon.className = active ? 'fas fa-bolt' : 'fas fa-exclamation-triangle';
            const txt = badge.querySelector('span') || badge.lastChild;
            if (txt && txt.nodeType === Node.TEXT_NODE) {
                txt.textContent = active ? ' GPU (QuickSync)' : ' CPU';
            }
            if (active) {
                badge.title = 'Intel QuickSync (iHD) — Aktywny';
            } else {
                badge.title = 'libx264 (CPU) — akceleracja GPU niedostępna';
            }
        });
    }

    /** Re-test GPU: resets encoder cache and probes H264/HEVC/VP9/AV1 via backend. */
    async function _retestGpu(container) {
        const btn = container.querySelector('#vs-gpu-retest-btn');
        const badge = container.querySelector('#vs-hw-badge-encoder');
        const pillsEl = container.querySelector('#vs-codec-pills');
        if (btn) { btn.disabled = true; btn.innerHTML = '<i class="fas fa-spinner fa-spin"></i> ' + t('Testuję GPU...'); }

        try {
            const res = await api('/video-station/gpu-retest', { method: 'POST' });
            if (!res) throw new Error('Brak odpowiedzi');

            // Update badge
            if (badge) {
                badge.classList.remove('vs-hw-badge-sw', 'vs-hw-badge-hw', 'vs-hw-badge-active');
                if (res.ok && res.is_hw) {
                    badge.classList.add('vs-hw-badge-active');
                    badge.title = res.tooltip || 'Intel QuickSync (iHD) — Aktywny';
                    badge.innerHTML = '<i class="fas fa-bolt"></i> GPU (' + escH(res.hw_encoder) + ')';
                } else {
                    const errIcon = res.error_code ? 'fa-exclamation-triangle' : 'fa-microchip';
                    badge.classList.add('vs-hw-badge-sw');
                    badge.title = res.tooltip || 'libx264 (CPU) — akceleracja GPU niedostępna';
                    badge.innerHTML = '<i class="fas ' + errIcon + '"></i> CPU (libx264)';
                }
            }

            // Codec pills
            if (pillsEl && res.codecs) {
                const codecList = [
                    { key: 'h264', label: 'H.264' },
                    { key: 'hevc', label: 'H.265/HEVC' },
                    { key: 'vp9',  label: 'VP9' },
                    { key: 'av1',  label: 'AV1' },
                ];
                pillsEl.innerHTML = codecList.map(c =>
                    '<span class="vs-codec-pill ' + (res.codecs[c.key] ? 'vs-codec-pill-ok' : 'vs-codec-pill-fail') + '" ' +
                    'title="' + c.label + ': ' + (res.codecs[c.key] ? t('obsługiwany przez GPU') : t('tylko CPU')) + '">' +
                    '<i class="fas fa-' + (res.codecs[c.key] ? 'check' : 'times') + '"></i> ' + c.label +
                    '</span>'
                ).join('');
            }

            // Also update all player badges in the window
            _updateHwBadges(bodyEl, res.ok && res.is_hw);

            if (res.ok) {
                toast(t('GPU aktywny!') + ' Intel QuickSync (iHD)', 'success');
                bodyEl.querySelector('.vs-hw-banner')?.remove();
            } else if (res.error_code === 'IHD_INIT_FAILED') {
                toast(t('iHD init failed — sprawdź sterowniki i LIBVA_DRIVER_NAME'), 'error');
                // Re-open wizard with fresh health data
                const health = await api('/video-station/hw-health').catch(() => null);
                if (health) _openHwWizard(bodyEl, health);
            } else {
                toast(t('GPU niedostępny') + (res.error_code ? ' (' + res.error_code + ')' : ''), 'warning');
            }
        } catch(e) {
            toast(t('Błąd testu GPU') + ': ' + e.message, 'error');
        } finally {
            if (btn) { btn.disabled = false; btn.innerHTML = '<i class="fas fa-vial"></i> ' + t('Re-test GPU'); }
        }
    }

    function _hwDiagRow(label, ok) {
        return '<div class="vs-hw-diag-row">' +
            '<span>' + label + '</span>' +
            '<span class="vs-hw-diag-' + (ok ? 'ok' : 'fail') + '">' +
              '<i class="fas fa-' + (ok ? 'check-circle' : 'times-circle') + '"></i> ' +
              (ok ? t('OK') : t('Brak')) +
            '</span>' +
        '</div>';
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
            case 'home':       toolbar.innerHTML = ''; loadHome(); break;
            case 'library':    renderLibraryToolbar(toolbar); loadLibrary(); break;
            case 'recent':     toolbar.innerHTML = '<div class="vs-toolbar-title">' + t('Ostatnio dodane') + '</div>'; loadRecent(); break;
            case 'history':    toolbar.innerHTML = '<div class="vs-toolbar-title"><i class="fas fa-history"></i> ' + t('Historia oglądania') + '</div>'; loadHistory(); break;
            case 'collections': toolbar.innerHTML = '<div class="vs-toolbar-title">' + t('Kolekcje') + '</div>'; loadCollections(); break;
            case 'folders':    toolbar.innerHTML = '<div class="vs-toolbar-title">' + t('Foldery biblioteki') + '</div>'; loadFolders(); break;
            case 'hidden':     toolbar.innerHTML = '<div class="vs-toolbar-title"><i class="fas fa-eye-slash"></i> ' + t('Ukryte filmy') + '</div>'; loadHidden(); break;
            case 'settings':   toolbar.innerHTML = '<div class="vs-toolbar-title"><i class="fas fa-sliders-h"></i> ' + t('Ustawienia') + '</div>'; loadSettingsSection(); break;
        }
        _exitSelectMode();
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
  '<select id="vs-watched-filter" class="vs-select">' +
    '<option value=""' + (currentWatched === '' ? ' selected' : '') + '>' + t('Wszystkie') + '</option>' +
    '<option value="0"' + (currentWatched === '0' ? ' selected' : '') + '>' + t('Nieobejrzane') + '</option>' +
    '<option value="1"' + (currentWatched === '1' ? ' selected' : '') + '>' + t('Obejrzane') + '</option>' +
  '</select>' +
  '<button id="vs-select-toggle" class="app-btn app-btn-sm" title="' + t('Zaznaczanie') + '"><i class="fas fa-check-square"></i></button>' +
  '<button class="app-btn app-btn-sm" title="' + t('Ustawienia, skanowanie, TMDb') + '" id="vs-lib-settings-btn"><i class="fas fa-sliders-h"></i></button>' +
'</div>';

        let searchTimer = null;
        bodyEl.querySelector('#vs-search').oninput = (e) => {
            clearTimeout(searchTimer);
            searchTimer = setTimeout(() => { currentQuery = e.target.value; libraryOffset = 0; loadLibrary(); }, 350);
        };
        bodyEl.querySelector('#vs-sort').onchange = (e) => {
            currentSort = e.target.value; libraryOffset = 0; loadLibrary();
        };
        bodyEl.querySelector('#vs-watched-filter').onchange = (e) => {
            currentWatched = e.target.value; libraryOffset = 0; loadLibrary();
        };
        bodyEl.querySelector('#vs-select-toggle').onclick = _toggleSelectMode;
        bodyEl.querySelector('#vs-lib-settings-btn').onclick = () => switchSection('settings');

        if (scanning) checkScanStatus();
    }

    async function loadLibrary() {
        const content = bodyEl.querySelector('#vs-content');
        if (!content) return;
        content.innerHTML = '<div class="vs-loading"><i class="fas fa-spinner fa-spin"></i></div>';

        const params = new URLSearchParams({
            offset: libraryOffset, limit: PAGE_SIZE,
            sort: currentSort, q: currentQuery, folder: currentFolder,
            watched: currentWatched,
        });
        const data = await api('/video-station/library?' + params);
        if (data.error) { content.innerHTML = '<div class="vs-empty">' + escH(data.error) + '</div>'; return; }

        libraryItems = data.items || [];
        libraryTotal = data.total || 0;

        if (!libraryItems.length && !currentQuery && !currentFolder && !currentWatched) {
            content.innerHTML =
                '<div class="vs-empty"><i class="fas fa-film"></i>' +
                '<p>' + t('Brak filmów w bibliotece') + '</p>' +
                '<p class="vs-empty-sub">' + t('Dodaj foldery i uruchom skanowanie') + '</p></div>';
            return;
        }

        let html = '';

        // Continue Watching section (only on first page with no filters)
        if (libraryOffset === 0 && !currentQuery && !currentFolder && !currentWatched) {
            const cw = await api('/video-station/continue-watching?limit=10');
            const cwItems = (cw && cw.items) ? cw.items : [];
            if (cwItems.length) {
                html += '<div class="vs-section-header"><i class="fas fa-play-circle"></i> ' + t('Kontynuuj oglądanie') + '</div>';
                html += '<div class="vs-horiz-scroll">';
                cwItems.forEach(v => {
                    const dur = formatDuration(v.duration);
                    const pct = v.duration && v.position ? Math.min(100, Math.round((v.position / v.duration) * 100)) : 0;
                    const imgSrc = v.poster_ok
                        ? '/api/video-station/poster/' + v.id + '?token=' + NAS.token
                        : '/api/video-station/thumb/' + v.id + '?token=' + NAS.token;
                    const remaining = v.duration && v.position ? formatDuration(v.duration - v.position) : '';
                    html +=
                    '<div class="vs-cw-card" data-id="' + v.id + '">' +
                      '<div class="vs-cw-thumb">' +
                        '<img src="' + imgSrc + '" loading="lazy" alt="" onerror="this.style.display=\'none\'">' +
                        '<div class="vs-thumb-placeholder"><i class="fas fa-film"></i></div>' +
                        '<div class="vs-cw-play"><i class="fas fa-play"></i></div>' +
                        (remaining ? '<span class="vs-cw-remaining">' + t('Pozostało') + ' ' + remaining + '</span>' : '') +
                        '<div class="vs-progress" style="width:' + pct + '%"></div>' +
                      '</div>' +
                      '<div class="vs-title">' + escH(v.title || v.filename || '') + '</div>' +
                    '</div>';
                });
                html += '</div>';
            }
        }

        if (!libraryItems.length) {
            html += '<div class="vs-empty"><i class="fas fa-filter"></i><p>' + t('Brak wyników') + '</p></div>';
        } else {
            html += renderGrid(libraryItems) + renderPagination();
        }
        content.innerHTML = html;
        attachGridEvents(content);
        attachPaginationEvents(content);

        // Continue Watching card events
        content.querySelectorAll('.vs-cw-card[data-id]').forEach(card => {
            card.onclick = () => openPlayer(card.dataset.id);
        });
    }

    /* ── home (Netflix-style) ──────────────────────────────────── */
    async function loadHome() {
        const content = bodyEl.querySelector('#vs-content');
        if (!content) return;
        content.innerHTML = '<div class="vs-loading"><i class="fas fa-spinner fa-spin"></i></div>';
        const data = await api('/video-station/home');
        if (data.error) { content.innerHTML = '<div class="vs-empty">' + escH(data.error) + '</div>'; return; }

        let html = '<div class="vs-home">';

        // Hero banner
        const hero = data.hero;
        if (hero) {
            const heroImg = hero.backdrop_ok
                ? '/api/video-station/backdrop/' + hero.id + '?token=' + NAS.token
                : (hero.poster_ok ? '/api/video-station/poster/' + hero.id + '?token=' + NAS.token : '');
            const stars = hero.tmdb_rating ? '★ ' + hero.tmdb_rating.toFixed(1) : '';
            const overview = (hero.tmdb_overview || '').slice(0, 220) + ((hero.tmdb_overview || '').length > 220 ? '…' : '');
            html += '<div class="vs-hero" data-vid="' + hero.id + '">';
            if (heroImg) html += '<div class="vs-hero-backdrop" style="background-image:url(' + heroImg + ')"></div>';
            html += '<div class="vs-hero-grad"></div>';
            html += '<div class="vs-hero-info">';
            if (stars) html += '<div class="vs-hero-rating"><i class="fas fa-star"></i> ' + escH(stars.replace('★ ', '')) + '</div>';
            html += '<h1 class="vs-hero-title">' + escH(hero.tmdb_title || hero.title) + '</h1>';
            if (hero.tmdb_year) html += '<span class="vs-hero-year">' + escH(hero.tmdb_year) + '</span>';
            if (_genreBadges(hero.tmdb_genres)) html += _genreBadges(hero.tmdb_genres);
            if (overview) html += '<p class="vs-hero-overview">' + escH(overview) + '</p>';
            html += '<div class="vs-hero-btns">';
            html += '<button class="vs-hero-play" data-vid="' + hero.id + '"><i class="fas fa-play"></i> ' + t('Odtwórz') + '</button>';
            html += '<button class="vs-hero-info-btn" data-vid="' + hero.id + '"><i class="fas fa-info-circle"></i> ' + t('Szczegóły') + '</button>';
            html += '</div></div></div>';
        }

        // Rows
        const rows = [];
        if (data.continue_watching && data.continue_watching.length)
            rows.push({ title: t('Oglądaj dalej'), items: data.continue_watching, cls: 'vs-row-continue' });
        if (data.recently_added && data.recently_added.length)
            rows.push({ title: t('Ostatnio dodane'), items: data.recently_added, cls: 'vs-row-recent' });
        (data.genres || []).forEach(g => {
            const name = _GENRE_MAP[g.genre_id] || t('Inne');
            if (g.items && g.items.length)
                rows.push({ title: name, items: g.items, cls: 'vs-row-genre' });
        });

        rows.forEach(row => {
            html += '<div class="vs-row ' + row.cls + '">';
            html += '<div class="vs-row-header"><h2 class="vs-row-title">' + escH(row.title) + '</h2>';
            html += '<div class="vs-row-arrows"><button class="vs-row-prev"><i class="fas fa-chevron-left"></i></button>'
                  + '<button class="vs-row-next"><i class="fas fa-chevron-right"></i></button></div></div>';
            html += '<div class="vs-row-track">';
            row.items.forEach(v => {
                const img = v.poster_ok
                    ? '/api/video-station/poster/' + v.id + '?token=' + NAS.token
                    : (v.thumb_ok ? '/api/video-station/thumb/' + v.id + '?token=' + NAS.token : '');
                const pct = v.duration > 0 ? Math.round(v.position / v.duration * 100) : 0;
                const rating = v.tmdb_rating ? v.tmdb_rating.toFixed(1) : '';
                const overview = (v.tmdb_overview || '').slice(0, 100) + ((v.tmdb_overview || '').length > 100 ? '…' : '');
                html += '<div class="vs-row-card" data-vid="' + v.id + '">';
                html += '<div class="vs-row-card-img">';
                if (img) html += '<img src="' + img + '" loading="lazy" />';
                else html += '<div class="vs-row-card-noimg"><i class="fas fa-film"></i></div>';
                if (pct > 5 && pct < 95) html += '<div class="vs-row-progress"><div style="width:' + pct + '%"></div></div>';
                html += '<div class="vs-row-card-hover">';
                html += '<button class="vs-row-play-btn" data-vid="' + v.id + '"><i class="fas fa-play"></i></button>';
                if (rating) html += '<div class="vs-row-hover-rating"><i class="fas fa-star"></i> ' + rating + '</div>';
                if (overview) html += '<div class="vs-row-hover-overview">' + escH(overview) + '</div>';
                html += _genreBadges(v.tmdb_genres);
                html += '</div></div>';
                html += '<div class="vs-row-card-title">' + escH(v.tmdb_title || v.title) + '</div>';
                html += '</div>';
            });
            html += '</div></div>';
        });

        if (!hero && !rows.length) {
            html += '<div class="vs-empty"><i class="fas fa-film"></i><p>' + t('Biblioteka jest pusta. Dodaj foldery i zeskanuj.') + '</p></div>';
        }
        html += '</div>';
        content.innerHTML = html;

        // Hero buttons
        content.querySelectorAll('.vs-hero-play').forEach(btn =>
            btn.onclick = () => openPlayer(parseInt(btn.dataset.vid)));
        content.querySelectorAll('.vs-hero-info-btn').forEach(btn =>
            btn.onclick = () => _showInfoModal(parseInt(btn.dataset.vid)));
        content.querySelectorAll('.vs-hero').forEach(el =>
            el.onclick = (e) => { if (e.target === el || el.querySelector('.vs-hero-backdrop') === e.target) openPlayer(parseInt(el.dataset.vid)); });

        // Row cards
        content.querySelectorAll('.vs-row-card').forEach(card => {
            card.onclick = (e) => {
                if (e.target.closest('.vs-row-play-btn')) return;
                openPlayer(parseInt(card.dataset.vid));
            };
        });
        content.querySelectorAll('.vs-row-play-btn').forEach(btn =>
            btn.onclick = (e) => { e.stopPropagation(); openPlayer(parseInt(btn.dataset.vid)); });

        // Scroll arrows
        content.querySelectorAll('.vs-row').forEach(rowEl => {
            const track = rowEl.querySelector('.vs-row-track');
            rowEl.querySelector('.vs-row-prev').onclick = () => track.scrollBy({ left: -track.offsetWidth * 0.8, behavior: 'smooth' });
            rowEl.querySelector('.vs-row-next').onclick = () => track.scrollBy({ left: track.offsetWidth * 0.8, behavior: 'smooth' });
        });
    }

    /* ── recent ────────────────────────────────────────────── */
    async function loadRecent() {
        const content = bodyEl.querySelector('#vs-content');
        if (!content) return;
        content.innerHTML = '<div class="vs-loading"><i class="fas fa-spinner fa-spin"></i></div>';

        const data = await api('/video-station/recent?limit=20');
        if (data.error) { content.innerHTML = '<div class="vs-empty">' + escH(data.error) + '</div>'; return; }

        const items = data.items || [];
        if (!items.length) {
            content.innerHTML = '<div class="vs-empty"><i class="fas fa-clock"></i><p>' + t('Brak ostatnich filmów') + '</p></div>';
            return;
        }
        content.innerHTML = renderGrid(items);
        attachGridEvents(content);
    }

    /* ── history ───────────────────────────────────────────── */
    async function loadHistory() {
        const content = bodyEl.querySelector('#vs-content');
        if (!content) return;
        content.innerHTML = '<div class="vs-loading"><i class="fas fa-spinner fa-spin"></i></div>';

        const data = await api('/video-station/history?limit=40');
        if (data.error) { content.innerHTML = '<div class="vs-empty">' + escH(data.error) + '</div>'; return; }

        const items = data.items || [];
        if (!items.length) {
            content.innerHTML = '<div class="vs-empty"><i class="fas fa-history"></i><p>' + t('Brak historii oglądania') + '</p><p class="vs-empty-sub">' + t('Obejrzane filmy pojawią się tutaj') + '</p></div>';
            return;
        }

        // Group by date
        const groups = {};
        const now = Date.now() / 1000;
        items.forEach(v => {
            const ts = v.last_watched_at || 0;
            let label;
            const diff = now - ts;
            if (diff < 86400) label = t('Dzisiaj');
            else if (diff < 172800) label = t('Wczoraj');
            else if (diff < 604800) label = t('Ten tydzień');
            else label = new Date(ts * 1000).toLocaleDateString('pl-PL', { month: 'long', year: 'numeric' });
            if (!groups[label]) groups[label] = [];
            groups[label].push(v);
        });

        let html = '';
        Object.entries(groups).forEach(([label, grpItems]) => {
            html += '<div class="vs-section-header"><i class="fas fa-calendar-alt"></i> ' + escH(label) + '</div>';
            html += renderGrid(grpItems, true);
        });
        content.innerHTML = html;
        attachGridEvents(content);
    }

    /* ── collections ───────────────────────────────────────── */
    async function loadCollections() {
        const content = bodyEl.querySelector('#vs-content');
        if (!content) return;
        content.innerHTML = '<div class="vs-loading"><i class="fas fa-spinner fa-spin"></i></div>';

        const data = await api('/video-station/collections');
        if (data.error) { content.innerHTML = '<div class="vs-empty">' + escH(data.error) + '</div>'; return; }

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

    /* ── settings section ──────────────────────────────────── */
    async function loadSettingsSection() {
        const content = bodyEl.querySelector('#vs-content');
        if (!content) return;
        content.innerHTML = '<div class="vs-loading"><i class="fas fa-spinner fa-spin"></i></div>';

        const [tmdbConf, encInfo, watcherInfo, foldersData] = await Promise.all([
            api('/video-station/tmdb-config'),
            api('/video-station/hls/encoder-info'),
            api('/video-station/watcher-status'),
            api('/video-station/folders'),
        ]);
        const folders = (foldersData && foldersData.folders) || [];

        content.innerHTML =
'<div class="vs-settings-page">' +

// ── TMDb ──
'<div class="vs-settings-card">' +
  '<div class="vs-settings-card-title"><i class="fas fa-magic"></i> TMDb — ' + t('rozpoznawanie filmów') + '</div>' +
  '<p class="vs-settings-desc">' + t('Klucz API z') + ' <a href="https://www.themoviedb.org/settings/api" target="_blank" style="color:var(--accent)">themoviedb.org</a></p>' +
  '<div class="vs-tmdb-key-row">' +
    '<input type="text" id="vs-tmdb-key" class="vs-input" placeholder="' + t('Klucz API TMDb (v3)') + '">' +
    '<button id="vs-tmdb-save" class="app-btn app-btn-sm app-btn-primary"><i class="fas fa-save"></i> ' + t('Zapisz') + '</button>' +
  '</div>' +
  '<div id="vs-tmdb-status" class="vs-tmdb-status" style="margin-top:8px">' +
    (tmdbConf && tmdbConf.has_key
      ? '<i class="fas fa-check-circle" style="color:var(--success)"></i> ' + t('Klucz aktywny') + ' (' + escH(tmdbConf.key_preview || '') + ')'
      : '<i class="fas fa-exclamation-circle" style="color:var(--warning)"></i> ' + t('Brak klucza')) +
  '</div>' +
'</div>' +

// ── Skanowanie ──
'<div class="vs-settings-card">' +
  '<div class="vs-settings-card-title"><i class="fas fa-sync-alt"></i> ' + t('Skanowanie biblioteki') + '</div>' +
  '<div class="vs-settings-row">' +
    '<label class="vs-tmdb-check" title="' + t('Rozpoznaj filmy przez TMDb podczas skanowania') + '">' +
      '<input type="checkbox" id="vs-settings-tmdb-check"' + (useTmdb ? ' checked' : '') + '> ' +
      '<i class="fas fa-magic"></i> ' + t('Użyj TMDb przy skanowaniu') +
    '</label>' +
  '</div>' +
  '<div class="vs-settings-actions">' +
    '<button id="vs-set-scan-btn" class="app-btn app-btn-primary"><i class="fas fa-sync-alt"></i> ' + t('Skanuj foldery') + '</button>' +
    '<button id="vs-set-reindex-btn" class="app-btn"><i class="fas fa-database"></i> ' + t('Reindeksuj metadane') + '</button>' +
    '<button id="vs-set-match-btn" class="app-btn"><i class="fas fa-wand-magic-sparkles"></i> ' + t('Dopasuj wszystko do TMDb') + '</button>' +
  '</div>' +
  '<div class="vs-scan-progress" id="vs-set-scan-bar" style="display:none">' +
    '<div class="vs-prog-bar"><div class="vs-prog-fill" id="vs-set-scan-fill"></div></div>' +
    '<span class="vs-scan-text" id="vs-set-scan-text"></span>' +
    '<button id="vs-set-scan-stop" class="app-btn app-btn-sm" style="color:var(--danger)"><i class="fas fa-stop"></i></button>' +
  '</div>' +
'</div>' +

// ── Enkoder ──
'<div class="vs-settings-card">' +
  '<div class="vs-settings-card-title"><i class="fas fa-microchip"></i> ' + t('Transkodowanie wideo') + '</div>' +
  '<div id="vs-hw-badge-encoder" class="vs-hw-badge ' +
       (encInfo && encInfo.type === 'hw' ? 'vs-hw-badge-hw' : 'vs-hw-badge-sw') + '" ' +
       'title="' + escH(encInfo && encInfo.tooltip ? encInfo.tooltip : (encInfo && encInfo.type === 'hw' ? 'Intel QuickSync (iHD) — Aktywny' : 'libx264 (CPU) — akceleracja GPU niedostępna')) + '">' +
    '<i class="fas fa-' + (encInfo && encInfo.type === 'hw' ? 'bolt' : (encInfo ? 'exclamation-triangle' : 'microchip')) + '"></i> ' +
    escH(encInfo && encInfo.label ? encInfo.label : 'libx264 (CPU)') +
  '</div>' +
  '<div id="vs-codec-pills" class="vs-codec-pills"></div>' +
  '<div class="vs-settings-actions" style="margin-top:10px">' +
    '<button id="vs-gpu-retest-btn" class="app-btn"><i class="fas fa-vial"></i> ' + t('Re-test GPU') + '</button>' +
  '</div>' +
  '<p class="vs-settings-desc">' + t('Akceleracja sprzętowa wykrywana automatycznie przy starcie serwera.') + '</p>' +
  '<p class="vs-settings-desc"><i class="fas fa-info-circle"></i> ' + t('Limit jednoczesnych sesji') + ': <strong>3</strong>. ' + t('Starsze sesje są automatycznie zamykane.') + '</p>' +
'</div>' +

// ── Foldery z rescan ──
'<div class="vs-settings-card">' +
  '<div class="vs-settings-card-title"><i class="fas fa-folder"></i> ' + t('Foldery biblioteki — rescan') + '</div>' +
  '<p class="vs-settings-desc">' + t('Kliknij "Skanuj folder", aby zaktualizować zawartość wybranego folderu.') + '</p>' +
  '<div id="vs-folder-rescan-list">' +
  (folders.length ? folders.map((f, i) =>
    '<div class="vs-folder-rescan-row">' +
      '<span class="vs-folder-rescan-path">' + escH(f) + '</span>' +
      '<button class="app-btn app-btn-sm vs-folder-rescan-btn" data-folder="' + escH(f) + '" data-idx="' + i + '">' +
        '<i class="fas fa-sync-alt"></i> ' + t('Skanuj folder') +
      '</button>' +
    '</div>'
  ).join('') : '<p class="vs-settings-desc">' + t('Brak skonfigurowanych folderów.') + '</p>') +
  '</div>' +
'</div>' +

// ── File Watcher ──
'<div class="vs-settings-card">' +
  '<div class="vs-settings-card-title"><i class="fas fa-eye"></i> ' + t('Automatyczne wykrywanie nowych filmów') + '</div>' +
  '<div class="vs-hw-badge ' + (watcherInfo && watcherInfo.ok ? 'vs-hw-badge-hw' : 'vs-hw-badge-sw') + '">' +
    '<i class="fas fa-' + (watcherInfo && watcherInfo.ok ? 'check-circle' : 'times-circle') + '"></i> ' +
    (watcherInfo && watcherInfo.ok ? t('Watcher aktywny') + ' (' + (watcherInfo.watched || []).length + ' ' + t('folderów') + ')' : t('Nieaktywny')) +
  '</div>' +
  '<p class="vs-settings-desc">' + t('Foldery sprawdzane co 60 sekund. Nowe pliki są dodawane automatycznie do biblioteki.') + '</p>' +
'</div>' +

'</div>';

        // TMDb save
        content.querySelector('#vs-tmdb-save').onclick = async () => {
            const key = content.querySelector('#vs-tmdb-key').value.trim();
            if (!key) { toast(t('Podaj klucz API'), 'warning'); return; }
            const res = await api('/video-station/tmdb-config', { method: 'POST', body: { api_key: key } });
            if (res.error) { toast(res.error, 'error'); return; }
            toast(t('Klucz TMDb zapisany!'), 'success');
            content.querySelector('#vs-tmdb-key').value = '';
            content.querySelector('#vs-tmdb-status').innerHTML =
                '<i class="fas fa-check-circle" style="color:var(--success)"></i> ' + t('Klucz aktywny');
        };
        content.querySelector('#vs-settings-tmdb-check').onchange = (e) => { useTmdb = e.target.checked; };

        // Scan
        content.querySelector('#vs-set-scan-btn').onclick = () => _settingsScan(content);
        content.querySelector('#vs-set-scan-stop').onclick = stopScan;
        content.querySelector('#vs-set-reindex-btn').onclick = async () => {
            const btn = content.querySelector('#vs-set-reindex-btn');
            btn.disabled = true; btn.innerHTML = '<i class="fas fa-spinner fa-spin"></i> ' + t('Reindeksowanie...');
            const res = await api('/video-station/rescan-metadata', { method: 'POST' });
            btn.disabled = false; btn.innerHTML = '<i class="fas fa-database"></i> ' + t('Reindeksuj metadane');
            if (res.error) { toast(res.error, 'error'); return; }
            toast(t('Odświeżono metadane dla {n} filmów', { n: res.updated || 0 }), 'success');
        };
        content.querySelector('#vs-set-match-btn').onclick = async () => {
            const btn = content.querySelector('#vs-set-match-btn');
            btn.disabled = true; btn.innerHTML = '<i class="fas fa-spinner fa-spin"></i> ' + t('Dopasowuję...');
            const res = await api('/video-station/tmdb-match-all', { method: 'POST' });
            btn.disabled = false; btn.innerHTML = '<i class="fas fa-wand-magic-sparkles"></i> ' + t('Dopasuj wszystko do TMDb');
            if (res.error) { toast(res.error, 'error'); return; }
            toast(t('Dopasowano {n} filmów', { n: res.matched || 0 }), 'success');
        };

        // Re-test GPU
        content.querySelector('#vs-gpu-retest-btn').onclick = () => _retestGpu(content);

        // Per-folder rescan buttons
        content.querySelectorAll('.vs-folder-rescan-btn').forEach(btn => {
            btn.onclick = async () => {
                const folder = btn.dataset.folder;
                btn.disabled = true;
                btn.innerHTML = '<i class="fas fa-spinner fa-spin"></i> ' + t('Skanuję...');
                const res = await api('/video-station/scan-folder', { method: 'POST', body: { folder, use_tmdb: useTmdb } });
                btn.disabled = false;
                btn.innerHTML = '<i class="fas fa-sync-alt"></i> ' + t('Skanuj folder');
                if (res.error) { toast(res.error, 'error'); return; }
                toast(t('Skanowanie folderu rozpoczęte w tle'), 'success');
            };
        });
    }

    async function _settingsScan(content) {
        const bar = content.querySelector('#vs-set-scan-bar');
        const fill = content.querySelector('#vs-set-scan-fill');
        const text = content.querySelector('#vs-set-scan-text');
        if (!bar) { startScan(); return; }
        bar.style.display = '';
        fill.style.width = '0%';
        text.textContent = t('Skanowanie...');
        const res = await api('/video-station/scan', { method: 'POST', body: { use_tmdb: useTmdb } });
        if (res.error) { toast(res.error, 'error'); bar.style.display = 'none'; return; }
        // progress via socket
    }

    /* ── folders settings ──────────────────────────────────── */
    async function loadFolders() {
        const content = bodyEl.querySelector('#vs-content');
        if (!content) return;
        content.innerHTML = '<div class="vs-loading"><i class="fas fa-spinner fa-spin"></i></div>';

        const data = await api('/video-station/folders');
        if (data.error) { content.innerHTML = '<div class="vs-empty">' + escH(data.error) + '</div>'; return; }

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
    '<button id="vs-rescan-meta-btn" class="app-btn app-btn-sm"><i class="fas fa-sync-alt"></i> ' + t('Odśwież metadane') + '</button>' +
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

        content.querySelector('#vs-rescan-meta-btn').onclick = async () => {
            const btn = content.querySelector('#vs-rescan-meta-btn');
            btn.disabled = true;
            btn.innerHTML = '<i class="fas fa-spinner fa-spin"></i> ' + t('Skanowanie...');
            const res = await api('/video-station/rescan-metadata', { method: 'POST' });
            btn.disabled = false;
            btn.innerHTML = '<i class="fas fa-sync-alt"></i> ' + t('Odśwież metadane');
            if (res.error) { toast(res.error, 'error'); return; }
            toast(t('Odświeżono metadane dla {n} filmów', { n: res.updated || 0 }), 'success');
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
    const _GENRE_MAP = {
        28:'Akcja',12:'Przygodowy',16:'Animacja',35:'Komedia',80:'Kryminał',
        99:'Dok.',18:'Dramat',10751:'Familijny',14:'Fantasy',36:'Historyczny',
        27:'Horror',10402:'Muzyczny',9648:'Tajemnica',10749:'Romans',878:'Sci-Fi',
        53:'Thriller',10752:'Wojenny',37:'Western',10759:'Akcja i Przygoda',
        10762:'Dla dzieci',10763:'Informacyjny',10764:'Reality',10765:'Sci-Fi & Fantasy',
        10766:'Telenowela',10767:'Talk-show',10768:'Wojenny i Polityczny',
    };
    function _genreBadges(tmdbGenres) {
        if (!tmdbGenres) return '';
        const ids = tmdbGenres.split(',');
        const names = ids.slice(0, 2).map(id => _GENRE_MAP[parseInt(id.trim())] || '').filter(Boolean);
        if (!names.length) return '';
        return '<div class="vs-genre-badges">' + names.map(n => '<span class="vs-genre-badge">' + escH(n) + '</span>').join('') + '</div>';
    }

    function renderGrid(items, showWatched) {
        let html = '<div class="vs-grid' + (selectMode ? ' vs-select-mode' : '') + '">';
        items.forEach(v => {
            const dur = formatDuration(v.duration);
            const pct = v.duration && v.position ? Math.min(100, Math.round((v.position / v.duration) * 100)) : 0;
            const res = v.height ? (v.height >= 2160 ? '4K' : v.height >= 1080 ? '1080p' : v.height >= 720 ? '720p' : v.height + 'p') : '';
            const codec = v.codec || '';
            const size = v.file_size ? formatBytes(v.file_size) : '';
            const meta = [v.tmdb_year || '', res, codec].filter(Boolean).join(' · ');
            const hasPoster = v.poster_ok;
            const imgSrc = hasPoster
                ? '/api/video-station/poster/' + v.id + '?token=' + NAS.token
                : '/api/video-station/thumb/' + v.id + '?token=' + NAS.token;
            const rating = v.tmdb_rating ? v.tmdb_rating.toFixed(1) : '';
            const sel = selectedIds.has(String(v.id));

            html +=
'<div class="vs-card' + (hasPoster ? ' vs-card-poster' : '') + (sel ? ' vs-selected' : '') + '" data-id="' + v.id + '">' +
  (selectMode ? '<div class="vs-checkbox' + (sel ? ' checked' : '') + '"><i class="fas fa-' + (sel ? 'check-square' : 'square') + '"></i></div>' : '') +
  '<div class="vs-thumb' + (hasPoster ? ' vs-thumb-poster' : '') + '">' +
    '<img src="' + imgSrc + '" loading="lazy" alt="" onerror="this.style.display=\'none\'">' +
    '<div class="vs-thumb-placeholder"><i class="fas fa-film"></i></div>' +
    (dur ? '<span class="vs-duration">' + dur + '</span>' : '') +
    (rating ? '<span class="vs-rating"><i class="fas fa-star"></i> ' + rating + '</span>' : '') +
    (pct > 0 && !v.watched ? '<div class="vs-progress" style="width:' + pct + '%"></div>' : '') +
    (v.watched ? '<span class="vs-watched"><i class="fas fa-check-circle"></i></span>' : '') +
    '<div class="vs-card-hover-play"><i class="fas fa-play"></i></div>' +
  '</div>' +
  '<div class="vs-title" title="' + escH(v.title || v.filename || '') + '">' + escH(v.title || v.filename || '') + '</div>' +
  (meta ? '<div class="vs-meta">' + escH(meta) + '</div>' : '') +
  _genreBadges(v.tmdb_genres) +
'</div>';
        });
        html += '</div>';
        return html;
    }

    function attachGridEvents(container) {
        container.querySelectorAll('.vs-card[data-id]').forEach(card => {
            card.onclick = (e) => {
                if (selectMode) {
                    _toggleSelection(card);
                    e.stopPropagation();
                    return;
                }
                openPlayer(card.dataset.id);
            };
            card.oncontextmenu = (e) => { e.preventDefault(); _showCardMenu(e, card.dataset.id); };
        });
    }

    function _showCardMenu(e, vid) {
        document.querySelectorAll('.vs-ctx-menu').forEach(m => m.remove());
        const menu = document.createElement('div');
        menu.className = 'vs-ctx-menu';
        const isHiddenSection = (activeSection === 'hidden');
        let items =
            '<div class="vs-ctx-item" data-action="select"><i class="fas fa-check-square"></i> ' + t('Zaznacz') + '</div>' +
            '<div class="vs-ctx-item" data-action="rename"><i class="fas fa-pen"></i> ' + t('Zmień nazwę') + '</div>' +
            '<div class="vs-ctx-item" data-action="tmdb"><i class="fas fa-wand-magic-sparkles"></i> ' + t('Pobierz z TMDb…') + '</div>' +
            '<div class="vs-ctx-item" data-action="unwatch"><i class="fas fa-eye-slash"></i> ' + t('Oznacz jako nieobejrzane') + '</div>' +
            '<div class="vs-ctx-item" data-action="info"><i class="fas fa-info-circle"></i> ' + t('Szczegóły') + '</div>';
        if (isHiddenSection) {
            items += '<div class="vs-ctx-item" data-action="unhide"><i class="fas fa-eye"></i> ' + t('Pokaż (odukryj)') + '</div>';
        } else {
            items += '<div class="vs-ctx-item" data-action="hide"><i class="fas fa-eye-slash"></i> ' + t('Ukryj') + '</div>';
        }
        items += '<div class="vs-ctx-item vs-ctx-danger" data-action="remove"><i class="fas fa-trash-alt"></i> ' + t('Usuń z biblioteki') + '</div>';
        menu.innerHTML = items;
        menu.style.left = e.clientX + 'px';
        menu.style.top = e.clientY + 'px';
        document.body.appendChild(menu);
        const dismiss = () => { menu.remove(); document.removeEventListener('click', dismiss); };
        setTimeout(() => document.addEventListener('click', dismiss), 0);
        menu.querySelectorAll('.vs-ctx-item').forEach(item => {
            item.onclick = async () => {
                dismiss();
                const action = item.dataset.action;
                if (action === 'select') {
                    if (!selectMode) _enterSelectMode();
                    selectedIds.add(String(vid));
                    _refreshGrid();
                } else if (action === 'rename') {
                    _showRenameDialog(vid);
                } else if (action === 'tmdb') {
                    _showTmdbSearchDialog(vid);
                } else if (action === 'unwatch') {
                    await api('/video-station/watched/' + vid, { method: 'POST', body: { watched: false, position: 0 } });
                    toast(t('Oznaczono jako nieobejrzane'), 'success');
                    _reloadSection();
                } else if (action === 'remove') {
                    if (!await confirmDialog(t('Usunąć film z biblioteki? Plik nie zostanie usunięty.'))) return;
                    const res = await api('/video-station/remove/' + vid, { method: 'POST' });
                    if (res.error) { toast(res.error, 'error'); return; }
                    toast(t('Usunięto z biblioteki'), 'success');
                    _reloadSection();
                } else if (action === 'info') {
                    _showInfoModal(vid);
                } else if (action === 'hide') {
                    await _batchHideAction([vid]);
                } else if (action === 'unhide') {
                    const res = await api('/video-station/batch', { method: 'POST', body: { ids: [vid], action: 'unhide' } });
                    if (res.error) { toast(res.error, 'error'); return; }
                    toast(t('Film przywrócony'), 'success');
                    _reloadSection();
                }
            };
        });
    }

    async function _showInfoModal(vid) {
        const info = await api('/video-station/info/' + vid);
        if (info.error) { toast(info.error, 'error'); return; }

        const modal = bodyEl.querySelector('#vs-info-modal');
        const content = bodyEl.querySelector('#vs-info-content');
        if (!modal || !content) return;

        const hasBackdrop = info.backdrop_ok;
        const hasPoster = info.poster_ok;
        const genres = (info.genre_names || []).join(', ') || '';
        const cast = info.tmdb_cast || '';
        const director = info.tmdb_director || '';
        const overview = info.tmdb_overview || '';
        const rating = info.tmdb_rating ? info.tmdb_rating.toFixed(1) : '';

        const posterSrc = hasPoster ? '/api/video-station/poster/' + vid + '?token=' + NAS.token : '';
        const backdropStyle = hasBackdrop
            ? 'background-image:url(/api/video-station/backdrop/' + vid + '?token=' + NAS.token + ')'
            : '';

        let html = '<div class="vs-info-backdrop" style="' + backdropStyle + '">';
        html += '<div class="vs-info-gradient"></div>';
        html += '<button class="vs-info-close" id="vs-info-close"><i class="fas fa-times"></i></button>';
        html += '<div class="vs-info-body">';

        if (posterSrc) {
            html += '<img class="vs-info-poster" src="' + posterSrc + '" alt="">';
        }

        html += '<div class="vs-info-details">';
        html += '<h2 class="vs-info-title">' + escH(info.title || info.filename) + '</h2>';

        // Meta row: year, duration, rating, resolution
        const metaParts = [];
        if (info.tmdb_year) metaParts.push(escH(info.tmdb_year));
        if (info.duration_fmt) metaParts.push(info.duration_fmt);
        if (rating) metaParts.push('<i class="fas fa-star" style="color:#fbbf24"></i> ' + rating);
        const res = info.height >= 2160 ? '4K' : info.height >= 1080 ? '1080p' : info.height >= 720 ? '720p' : '';
        if (res) metaParts.push(res);
        if (info.file_size) metaParts.push(formatBytes(info.file_size));
        if (metaParts.length) {
            html += '<div class="vs-info-meta">' + metaParts.join(' <span class="vs-info-dot">·</span> ') + '</div>';
        }

        if (genres) {
            html += '<div class="vs-info-genres">' + escH(genres) + '</div>';
        }

        if (overview) {
            html += '<div class="vs-info-overview">' + escH(overview) + '</div>';
        }

        if (director) {
            html += '<div class="vs-info-credit"><span class="vs-info-label">' + t('Reżyser') + ':</span> ' + escH(director) + '</div>';
        }
        if (cast) {
            html += '<div class="vs-info-credit"><span class="vs-info-label">' + t('Obsada') + ':</span> ' + escH(cast) + '</div>';
        }

        // Technical details
        html += '<div class="vs-info-tech">';
        html += '<span>' + escH(info.codec || '-') + '</span>';
        html += '<span>' + escH(info.audio_codec || '-') + '</span>';
        if (info.width && info.height) html += '<span>' + info.width + '×' + info.height + '</span>';
        if (info.needs_transcode) html += '<span class="vs-info-tc">' + t('Transkodowanie') + '</span>';
        html += '</div>';

        // Play button
        html += '<div class="vs-info-actions">';
        html += '<button class="vs-info-play" id="vs-info-play"><i class="fas fa-play"></i> ' + t('Odtwórz') + '</button>';
        html += '<button class="vs-info-tmdb-btn" id="vs-info-tmdb"><i class="fas fa-wand-magic-sparkles"></i> ' + t('Pobierz z TMDb') + '</button>';
        html += '<button class="vs-info-rename-btn" id="vs-info-rename"><i class="fas fa-pen"></i> ' + t('Zmień nazwę') + '</button>';
        html += '</div>';

        html += '</div></div></div>';

        content.innerHTML = html;
        modal.style.display = 'flex';

        modal.querySelector('#vs-info-close').onclick = () => { modal.style.display = 'none'; };
        modal.querySelector('#vs-info-play').onclick = () => { modal.style.display = 'none'; openPlayer(vid); };
        modal.querySelector('#vs-info-tmdb').onclick = () => { modal.style.display = 'none'; _showTmdbSearchDialog(vid); };
        modal.querySelector('#vs-info-rename').onclick = () => { modal.style.display = 'none'; _showRenameDialog(vid); };
        modal.onclick = (e) => { if (e.target === modal) modal.style.display = 'none'; };
    }

    /* ── rename dialog ─────────────────────────────────────────── */
    function _showRenameDialog(vid) {
        const item = libraryItems.find(i => String(i.id) === String(vid));
        const currentName = item ? item.filename : '';
        const overlay = document.createElement('div');
        overlay.className = 'vs-modal-overlay vs-rename-overlay';
        overlay.innerHTML =
            '<div class="vs-rename-dialog">' +
            '<div class="vs-rename-header"><span>' + t('Zmień nazwę pliku') + '</span>' +
            '<button class="vs-rename-close"><i class="fas fa-times"></i></button></div>' +
            '<div class="vs-rename-body">' +
            '<label class="vs-rename-label">' + t('Nowa nazwa pliku') + '</label>' +
            '<input class="vs-rename-input" id="vs-rename-input" type="text" value="' + (currentName || '').replace(/"/g, '&quot;') + '" />' +
            '<p class="vs-rename-hint">' + t('Rozszerzenie zostanie zachowane automatycznie') + '</p>' +
            '</div>' +
            '<div class="vs-rename-footer">' +
            '<button class="vs-btn-secondary vs-rename-cancel">' + t('Anuluj') + '</button>' +
            '<button class="vs-btn-primary vs-rename-confirm"><i class="fas fa-check"></i> ' + t('Zmień nazwę') + '</button>' +
            '</div></div>';
        document.body.appendChild(overlay);
        const input = overlay.querySelector('#vs-rename-input');
        const extIdx = currentName ? currentName.lastIndexOf('.') : -1;
        input.focus();
        if (extIdx > 0) input.setSelectionRange(0, extIdx);

        overlay.querySelector('.vs-rename-close').onclick = () => overlay.remove();
        overlay.querySelector('.vs-rename-cancel').onclick = () => overlay.remove();
        overlay.onclick = (e) => { if (e.target === overlay) overlay.remove(); };
        overlay.querySelector('.vs-rename-confirm').onclick = async () => {
            const newName = input.value.trim();
            if (!newName) return;
            try {
                const res = await api('/video-station/rename/' + vid, { method: 'POST', body: { name: newName } });
                if (res.error) { toast(res.error, 'error'); return; }
                toast(t('Zmieniono nazwę na: ') + res.filename, 'success');
                overlay.remove();
                _reloadSection();
            } catch (e) { toast(t('Błąd zmiany nazwy'), 'error'); }
        };
    }

    /* ── TMDb search dialog ─────────────────────────────────────── */
    function _showTmdbSearchDialog(vid) {
        const item = libraryItems.find(i => String(i.id) === String(vid));
        const initialQuery = item ? (item.title || item.filename || '').replace(/\.[^.]+$/, '') : '';
        const overlay = document.createElement('div');
        overlay.className = 'vs-modal-overlay vs-tmdb-overlay';
        overlay.innerHTML =
            '<div class="vs-tmdb-dialog">' +
            '<div class="vs-tmdb-header"><span><i class="fas fa-wand-magic-sparkles"></i> ' + t('Wyszukaj w TMDb') + '</span>' +
            '<button class="vs-tmdb-close"><i class="fas fa-times"></i></button></div>' +
            '<div class="vs-tmdb-search-row">' +
            '<input class="vs-tmdb-search-input" id="vs-tmdb-q" type="text" placeholder="' + t('Tytuł filmu lub serialu…') + '" value="' + initialQuery.replace(/"/g, '&quot;') + '" />' +
            '<button class="vs-btn-primary vs-tmdb-search-btn" id="vs-tmdb-search-btn"><i class="fas fa-search"></i> ' + t('Szukaj') + '</button>' +
            '</div>' +
            '<div class="vs-tmdb-results" id="vs-tmdb-results"><p class="vs-tmdb-hint">' + t('Wpisz tytuł i kliknij Szukaj') + '</p></div>' +
            '</div>';
        document.body.appendChild(overlay);
        overlay.querySelector('.vs-tmdb-close').onclick = () => overlay.remove();
        overlay.onclick = (e) => { if (e.target === overlay) overlay.remove(); };

        const doSearch = async () => {
            const q = overlay.querySelector('#vs-tmdb-q').value.trim();
            if (!q) return;
            const resultsEl = overlay.querySelector('#vs-tmdb-results');
            resultsEl.innerHTML = '<p class="vs-tmdb-hint"><i class="fas fa-spinner fa-spin"></i> ' + t('Szukam…') + '</p>';
            try {
                const res = await api('/video-station/tmdb-search-list?q=' + encodeURIComponent(q));
                if (res.error) { resultsEl.innerHTML = '<p class="vs-tmdb-hint vs-tmdb-error">' + res.error + '</p>'; return; }
                if (!res.results || !res.results.length) { resultsEl.innerHTML = '<p class="vs-tmdb-hint">' + t('Brak wyników') + '</p>'; return; }
                resultsEl.innerHTML = res.results.map(r => {
                    const poster = r.poster_path ? 'https://image.tmdb.org/t/p/w92' + r.poster_path : '';
                    const year = (r.release_date || r.first_air_date || '').slice(0, 4);
                    const badge = r.media_type === 'tv'
                        ? '<span class="vs-tmdb-type-badge vs-tmdb-tv">TV</span>'
                        : '<span class="vs-tmdb-type-badge vs-tmdb-movie">Film</span>';
                    return '<div class="vs-tmdb-result" data-id="' + r.id + '" data-type="' + r.media_type + '">' +
                        (poster ? '<img class="vs-tmdb-poster" src="' + poster + '" loading="lazy" />'
                                : '<div class="vs-tmdb-poster vs-tmdb-no-poster"><i class="fas fa-film"></i></div>') +
                        '<div class="vs-tmdb-result-info">' +
                        '<div class="vs-tmdb-result-title">' + (r.title || r.name || '') + ' ' + badge + '</div>' +
                        '<div class="vs-tmdb-result-year">' + (year || '') + '</div>' +
                        '<div class="vs-tmdb-result-overview">' + (r.overview || '').slice(0, 120) + ((r.overview || '').length > 120 ? '…' : '') + '</div>' +
                        '</div></div>';
                }).join('');
                resultsEl.querySelectorAll('.vs-tmdb-result').forEach(el => {
                    el.onclick = async () => {
                        el.style.opacity = '0.5';
                        try {
                            const r2 = await api('/video-station/tmdb-apply/' + vid, {
                                method: 'POST',
                                body: JSON.stringify({ tmdb_id: el.dataset.id, type: el.dataset.type })
                            });
                            if (r2.error) { toast(r2.error, 'error'); el.style.opacity = ''; return; }
                            toast(t('Metadane zaktualizowane!'), 'success');
                            overlay.remove();
                            _reloadSection();
                        } catch(e) { toast(t('Błąd aktualizacji metadanych'), 'error'); el.style.opacity = ''; }
                    };
                });
            } catch(e) { resultsEl.innerHTML = '<p class="vs-tmdb-hint vs-tmdb-error">' + t('Błąd wyszukiwania') + '</p>'; }
        };

        overlay.querySelector('#vs-tmdb-search-btn').onclick = doSearch;
        overlay.querySelector('#vs-tmdb-q').onkeydown = (e) => { if (e.key === 'Enter') doSearch(); };
        if (initialQuery) doSearch();
    }


    /* ── multi-select ──────────────────────────────────────── */
    function _toggleSelectMode() {
        if (selectMode) _exitSelectMode();
        else _enterSelectMode();
    }

    function _enterSelectMode() {
        selectMode = true;
        selectedIds.clear();
        const btn = bodyEl && bodyEl.querySelector('#vs-select-toggle');
        if (btn) btn.classList.add('active');
        _refreshGrid();
        _updateBatchBar();
    }

    function _exitSelectMode() {
        selectMode = false;
        selectedIds.clear();
        const btn = bodyEl && bodyEl.querySelector('#vs-select-toggle');
        if (btn) btn.classList.remove('active');
        const bar = bodyEl && bodyEl.querySelector('#vs-batch-bar');
        if (bar) bar.style.display = 'none';
        _refreshGrid();
    }

    function _toggleSelection(card) {
        const id = String(card.dataset.id);
        if (selectedIds.has(id)) selectedIds.delete(id);
        else selectedIds.add(id);
        // Update just this card's visual state
        card.classList.toggle('vs-selected', selectedIds.has(id));
        const cb = card.querySelector('.vs-checkbox');
        if (cb) {
            cb.classList.toggle('checked', selectedIds.has(id));
            cb.innerHTML = '<i class="fas fa-' + (selectedIds.has(id) ? 'check-square' : 'square') + '"></i>';
        }
        _updateBatchBar();
    }

    function _refreshGrid() {
        const content = bodyEl && bodyEl.querySelector('#vs-content');
        if (!content) return;
        const grid = content.querySelector('.vs-grid');
        if (!grid) return;
        grid.classList.toggle('vs-select-mode', selectMode);
        grid.querySelectorAll('.vs-card[data-id]').forEach(card => {
            const id = String(card.dataset.id);
            const sel = selectedIds.has(id);
            card.classList.toggle('vs-selected', sel);
            let cb = card.querySelector('.vs-checkbox');
            if (selectMode && !cb) {
                cb = document.createElement('div');
                cb.className = 'vs-checkbox' + (sel ? ' checked' : '');
                cb.innerHTML = '<i class="fas fa-' + (sel ? 'check-square' : 'square') + '"></i>';
                card.prepend(cb);
            } else if (!selectMode && cb) {
                cb.remove();
            } else if (cb) {
                cb.classList.toggle('checked', sel);
                cb.innerHTML = '<i class="fas fa-' + (sel ? 'check-square' : 'square') + '"></i>';
            }
        });
    }

    function _updateBatchBar() {
        const bar = bodyEl && bodyEl.querySelector('#vs-batch-bar');
        if (!bar) return;
        if (!selectMode || selectedIds.size === 0) {
            bar.style.display = 'none';
            return;
        }
        const isHiddenSection = (activeSection === 'hidden');
        bar.style.display = 'flex';
        bar.innerHTML =
            '<span class="vs-batch-count">' + t('Zaznaczono') + ': <b>' + selectedIds.size + '</b></span>' +
            '<button class="app-btn app-btn-sm vs-batch-btn" data-action="select-all"><i class="fas fa-check-double"></i> ' + t('Zaznacz wszystkie') + '</button>' +
            '<button class="app-btn app-btn-sm vs-batch-btn" data-action="watched"><i class="fas fa-eye"></i> ' + t('Obejrzane') + '</button>' +
            '<button class="app-btn app-btn-sm vs-batch-btn" data-action="unwatched"><i class="fas fa-eye-slash"></i> ' + t('Nieobejrzane') + '</button>' +
            (isHiddenSection
                ? '<button class="app-btn app-btn-sm vs-batch-btn" data-action="unhide"><i class="fas fa-eye"></i> ' + t('Pokaż') + '</button>'
                : '<button class="app-btn app-btn-sm vs-batch-btn" data-action="hide"><i class="fas fa-lock"></i> ' + t('Ukryj') + '</button>') +
            '<button class="app-btn app-btn-sm vs-batch-btn vs-batch-danger" data-action="remove"><i class="fas fa-trash-alt"></i> ' + t('Usuń') + '</button>' +
            '<button class="app-btn app-btn-sm vs-batch-btn" data-action="cancel"><i class="fas fa-times"></i> ' + t('Anuluj') + '</button>';
        bar.querySelectorAll('.vs-batch-btn').forEach(btn => {
            btn.onclick = () => _onBatchAction(btn.dataset.action);
        });
    }

    async function _onBatchAction(action) {
        if (action === 'cancel') { _exitSelectMode(); return; }
        if (action === 'select-all') {
            const grid = bodyEl && bodyEl.querySelector('.vs-grid');
            if (grid) {
                grid.querySelectorAll('.vs-card[data-id]').forEach(card => {
                    selectedIds.add(String(card.dataset.id));
                });
                _refreshGrid();
                _updateBatchBar();
            }
            return;
        }
        const ids = Array.from(selectedIds).map(Number);
        if (!ids.length) return;
        if (action === 'remove') {
            if (!await confirmDialog(t('Usunąć {n} filmów z biblioteki?', { n: ids.length }))) return;
        }
        if (action === 'hide') {
            await _batchHideAction(ids);
            return;
        }
        const res = await api('/video-station/batch', { method: 'POST', body: { ids, action } });
        if (res.error) { toast(res.error, 'error'); return; }
        const msgs = { watched: 'Oznaczono jako obejrzane', unwatched: 'Oznaczono jako nieobejrzane', remove: 'Usunięto z biblioteki', unhide: 'Filmy przywrócone' };
        toast(t(msgs[action] || 'Gotowe'), 'success');
        _exitSelectMode();
        _reloadSection();
    }

    async function _batchHideAction(ids) {
        if (!hidePasswordSet) {
            _showSetPasswordPrompt(async () => {
                const res = await api('/video-station/batch', { method: 'POST', body: { ids, action: 'hide' } });
                if (res.error) { toast(res.error, 'error'); return; }
                toast(t('Ukryto {n} filmów', { n: ids.length }), 'success');
                hiddenCount += ids.length;
                _updateHiddenBadge();
                _exitSelectMode();
                _reloadSection();
            });
            return;
        }
        const res = await api('/video-station/batch', { method: 'POST', body: { ids, action: 'hide' } });
        if (res.error) { toast(res.error, 'error'); return; }
        toast(t('Ukryto {n} filmów', { n: ids.length }), 'success');
        hiddenCount += ids.length;
        _updateHiddenBadge();
        _exitSelectMode();
        _reloadSection();
    }

    function _reloadSection() {
        if (activeSection === 'library') loadLibrary();
        else if (activeSection === 'recent') loadRecent();
        else if (activeSection === 'history') loadHistory();
        else if (activeSection === 'collections') loadCollections();
        else if (activeSection === 'hidden') loadHidden();
    }

    function _updateHiddenBadge() {
        if (!bodyEl) return;
        const navItem = bodyEl.querySelector('.vs-nav-item[data-section="hidden"]');
        if (!navItem) return;
        let badge = navItem.querySelector('.vs-nav-badge');
        if (hiddenCount > 0) {
            if (!badge) {
                badge = document.createElement('span');
                badge.className = 'vs-nav-badge';
                navItem.appendChild(badge);
            }
            badge.textContent = hiddenCount;
        } else if (badge) {
            badge.remove();
        }
    }

    /* ── hidden section ────────────────────────────────────── */
    async function loadHidden() {
        const content = bodyEl.querySelector('#vs-content');
        if (!content) return;

        if (!hidePasswordSet) {
            content.innerHTML =
                '<div class="vs-empty"><i class="fas fa-lock"></i>' +
                '<p>' + t('Nie ustawiono hasła ukrywania') + '</p>' +
                '<p class="vs-empty-sub">' + t('Ustaw hasło aby móc ukrywać filmy') + '</p>' +
                '<button id="vs-set-hide-pw" class="app-btn app-btn-primary" style="margin-top:16px"><i class="fas fa-key"></i> ' + t('Ustaw hasło') + '</button></div>';
            content.querySelector('#vs-set-hide-pw').onclick = () => _showSetPasswordPrompt(() => loadHidden());
            return;
        }

        if (!hideUnlocked) {
            content.innerHTML =
                '<div class="vs-empty"><i class="fas fa-lock"></i>' +
                '<p>' + t('Ukryte filmy są zablokowane') + '</p>' +
                '<button id="vs-unlock-hidden" class="app-btn app-btn-primary" style="margin-top:16px"><i class="fas fa-unlock"></i> ' + t('Odblokuj') + '</button></div>';
            content.querySelector('#vs-unlock-hidden').onclick = () => _showUnlockPrompt();
            return;
        }

        content.innerHTML = '<div class="vs-loading"><i class="fas fa-spinner fa-spin"></i></div>';
        const params = new URLSearchParams({ offset: libraryOffset, limit: PAGE_SIZE, sort: currentSort, show_hidden: '1' });
        const data = await api('/video-station/library?' + params);
        if (data.error) { content.innerHTML = '<div class="vs-empty">' + escH(data.error) + '</div>'; return; }

        libraryItems = data.items || [];
        libraryTotal = data.total || 0;
        hiddenCount = libraryTotal;
        _updateHiddenBadge();

        if (!libraryItems.length) {
            content.innerHTML =
                '<div class="vs-empty"><i class="fas fa-eye-slash"></i>' +
                '<p>' + t('Brak ukrytych filmów') + '</p>' +
                '<button id="vs-lock-hidden" class="app-btn app-btn-sm" style="margin-top:16px"><i class="fas fa-lock"></i> ' + t('Zablokuj') + '</button></div>';
            const lockBtn = content.querySelector('#vs-lock-hidden');
            if (lockBtn) lockBtn.onclick = async () => {
                await api('/video-station/hide-lock', { method: 'POST' });
                hideUnlocked = false;
                loadHidden();
            };
            return;
        }

        let html = '<div style="display:flex;justify-content:flex-end;padding:0 8px 8px">' +
            '<button id="vs-lock-hidden" class="app-btn app-btn-sm"><i class="fas fa-lock"></i> ' + t('Zablokuj') + '</button></div>';
        html += renderGrid(libraryItems) + renderPagination();
        content.innerHTML = html;
        attachGridEvents(content);
        attachPaginationEvents(content);
        const lockBtn = content.querySelector('#vs-lock-hidden');
        if (lockBtn) lockBtn.onclick = async () => {
            await api('/video-station/hide-lock', { method: 'POST' });
            hideUnlocked = false;
            loadHidden();
        };
    }

    function _showSetPasswordPrompt(onSuccess) {
        const html =
            '<div class="vs-modal-overlay" id="vs-pw-modal">' +
            '<div class="vs-modal-box">' +
            '<h3><i class="fas fa-key"></i> ' + t('Ustaw hasło ukrywania') + '</h3>' +
            '<input type="password" id="vs-pw-input" class="vs-input" placeholder="' + t('Hasło (min. 4 znaki)') + '" style="width:100%;margin:12px 0">' +
            '<div style="display:flex;gap:8px;justify-content:flex-end">' +
            '<button class="app-btn app-btn-sm" id="vs-pw-cancel">' + t('Anuluj') + '</button>' +
            '<button class="app-btn app-btn-sm app-btn-primary" id="vs-pw-ok">' + t('Zapisz') + '</button>' +
            '</div></div></div>';
        bodyEl.insertAdjacentHTML('beforeend', html);
        const modal = bodyEl.querySelector('#vs-pw-modal');
        const input = bodyEl.querySelector('#vs-pw-input');
        input.focus();
        bodyEl.querySelector('#vs-pw-cancel').onclick = () => modal.remove();
        bodyEl.querySelector('#vs-pw-ok').onclick = async () => {
            const pw = input.value;
            if (!pw || pw.length < 4) { toast(t('Hasło za krótkie'), 'warning'); return; }
            const res = await api('/video-station/hide-password', { method: 'POST', body: { password: pw } });
            if (res.error) { toast(res.error, 'error'); return; }
            hidePasswordSet = true;
            toast(t('Hasło ustawione'), 'success');
            modal.remove();
            if (onSuccess) onSuccess();
        };
        input.onkeydown = (e) => { if (e.key === 'Enter') bodyEl.querySelector('#vs-pw-ok').click(); };
    }

    function _showUnlockPrompt() {
        const html =
            '<div class="vs-modal-overlay" id="vs-pw-modal">' +
            '<div class="vs-modal-box">' +
            '<h3><i class="fas fa-unlock"></i> ' + t('Odblokuj ukryte filmy') + '</h3>' +
            '<input type="password" id="vs-pw-input" class="vs-input" placeholder="' + t('Hasło') + '" style="width:100%;margin:12px 0">' +
            '<div style="display:flex;gap:8px;justify-content:flex-end">' +
            '<button class="app-btn app-btn-sm" id="vs-pw-cancel">' + t('Anuluj') + '</button>' +
            '<button class="app-btn app-btn-sm app-btn-primary" id="vs-pw-ok">' + t('Odblokuj') + '</button>' +
            '</div></div></div>';
        bodyEl.insertAdjacentHTML('beforeend', html);
        const modal = bodyEl.querySelector('#vs-pw-modal');
        const input = bodyEl.querySelector('#vs-pw-input');
        input.focus();
        bodyEl.querySelector('#vs-pw-cancel').onclick = () => modal.remove();
        bodyEl.querySelector('#vs-pw-ok').onclick = async () => {
            const pw = input.value;
            if (!pw) return;
            const res = await api('/video-station/hide-unlock', { method: 'POST', body: { password: pw } });
            if (res.error) { toast(res.error, 'error'); return; }
            hideUnlocked = true;
            toast(t('Odblokowano'), 'success');
            modal.remove();
            loadHidden();
        };
        input.onkeydown = (e) => { if (e.key === 'Enter') bodyEl.querySelector('#vs-pw-ok').click(); };
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
    let _transcoding = false;
    let _currentVid = null;
    let _currentAudioIdx = null;
    let _hlsSessionId = null;
    let _hlsInstance = null;
    let _knownDuration = 0;
    let _startOffset = 0;
    let _heartbeatTimer = null;
    let _ctrlHideTimer = null;
    let _ctrlVisible = true;

    function _buildStreamUrl(vid) {
        return '/api/video-station/stream/' + vid + '?token=' + NAS.token;
    }

    /** Load hls.js from CDN if not already loaded. Returns a Promise. */
    function _ensureHlsJs() {
        if (window.Hls) return Promise.resolve();
        return new Promise((resolve, reject) => {
            const s = document.createElement('script');
            s.src = 'https://cdn.jsdelivr.net/npm/hls.js@1/dist/hls.min.js';
            s.onload = resolve;
            s.onerror = reject;
            document.head.appendChild(s);
        });
    }

    /** Thumbstrip seek preview using VTT sprite from backend */
    let _thumbVtt = null;
    let _thumbImg = null;
    let _thumbVid = null;

    async function _initThumbstrip(vid, videoEl, duration) {
        const preview = bodyEl.querySelector('#vs-thumbstrip-preview');
        const canvas = bodyEl.querySelector('#vs-thumbstrip-canvas');
        const timeEl = bodyEl.querySelector('#vs-thumbstrip-time');
        if (!preview || !canvas || !videoEl) return;

        _thumbVtt = null;
        _thumbImg = null;
        _thumbVid = vid;

        // Load VTT metadata
        try {
            const vttUrl = '/api/video-station/thumbstrip/' + vid + '?token=' + NAS.token;
            const resp = await fetch(vttUrl);
            if (!resp.ok) return; // 202 = not ready yet
            const vttText = await resp.text();
            _thumbVtt = _parseVtt(vttText, vttUrl);
            // Load sprite image
            if (_thumbVtt && _thumbVtt.length > 0 && _thumbVtt[0].imgUrl) {
                _thumbImg = new Image();
                _thumbImg.src = _thumbVtt[0].imgUrl + '?token=' + NAS.token;
            }
        } catch (_) { return; }

        // Seek-bar preview: listen on progress bar mousemove
        const seekBar = videoEl.parentElement ? videoEl.parentElement.querySelector('input[type=range]') : null;
        // Since we use native controls, hook into mousemove on the overlay bottom area
        const overlay = bodyEl.querySelector('#vs-player-overlay');
        if (!overlay) return;

        overlay.addEventListener('mousemove', _onSeekHover);

        function _onSeekHover(e) {
            if (!_thumbVtt || !_thumbImg || !_thumbImg.complete || _thumbVid !== vid) return;
            const rect = overlay.getBoundingClientRect();
            const relY = e.clientY - rect.top;
            // Only show preview when mouse is in bottom 60px (near seek bar)
            if (relY < rect.height - 80 || relY > rect.height - 10) {
                preview.style.display = 'none'; return;
            }
            const relX = e.clientX - rect.left;
            const fraction = Math.max(0, Math.min(1, relX / rect.width));
            const seekTime = fraction * duration;
            const entry = _thumbVtt.find(t => seekTime >= t.start && seekTime <= t.end)
                || _thumbVtt[0];
            if (!entry) { preview.style.display = 'none'; return; }

            const ctx = canvas.getContext('2d');
            canvas.width = entry.w || 160;
            canvas.height = entry.h || 90;
            try {
                ctx.drawImage(_thumbImg, entry.x || 0, entry.y || 0, entry.w || 160, entry.h || 90, 0, 0, canvas.width, canvas.height);
            } catch (_) { preview.style.display = 'none'; return; }

            timeEl.textContent = formatDuration(seekTime);
            const px = Math.max(80, Math.min(rect.width - 80, e.clientX - rect.left));
            preview.style.left = (px - canvas.width / 2) + 'px';
            preview.style.bottom = '70px';
            preview.style.display = 'flex';
        }

        overlay._tsCleanup = () => overlay.removeEventListener('mousemove', _onSeekHover);
        // Hide when mouse leaves bottom area
        overlay.addEventListener('mouseleave', () => { preview.style.display = 'none'; });
    }

    function _parseVtt(text, baseUrl) {
        const lines = text.split('\n');
        const entries = [];
        const base = baseUrl.replace(/[^/]*$/, '');
        let i = 0;
        while (i < lines.length) {
            const line = lines[i].trim();
            if (line.includes('-->')) {
                const [startStr, endStr] = line.split('-->').map(s => s.trim());
                const start = _vttTime(startStr);
                const end = _vttTime(endStr);
                const imgLine = (lines[i + 1] || '').trim();
                if (imgLine) {
                    const hashIdx = imgLine.indexOf('#xywh=');
                    let imgUrl, x = 0, y = 0, w = 160, h = 90;
                    if (hashIdx !== -1) {
                        imgUrl = imgLine.slice(0, hashIdx);
                        const parts = imgLine.slice(hashIdx + 6).split(',').map(Number);
                        [x, y, w, h] = parts;
                    } else {
                        imgUrl = imgLine;
                    }
                    if (!imgUrl.startsWith('http') && !imgUrl.startsWith('/')) imgUrl = base + imgUrl;
                    entries.push({ start, end, imgUrl, x, y, w, h });
                }
                i += 2;
            } else { i++; }
        }
        return entries;
    }

    function _vttTime(s) {
        const parts = s.replace(',', '.').split(':').map(Number);
        if (parts.length === 3) return parts[0] * 3600 + parts[1] * 60 + parts[2];
        if (parts.length === 2) return parts[0] * 60 + parts[1];
        return parts[0];
    }

    async function openPlayer(vid) {
        const info = await api('/video-station/info/' + vid);
        if (info.error) { toast(info.error, 'error'); return; }

        const overlay  = bodyEl.querySelector('#vs-player-overlay');
        const video    = bodyEl.querySelector('#vs-player-video');
        const title    = bodyEl.querySelector('#vs-player-title');
        const badge    = bodyEl.querySelector('#vs-player-badge');
        const audioSel = bodyEl.querySelector('#vs-audio-select');
        const subSel   = bodyEl.querySelector('#vs-sub-select');
        const speedSel = bodyEl.querySelector('#vs-speed-select');
        if (!overlay || !video) return;

        const needsTranscode = !!info.needs_transcode;
        _transcoding = needsTranscode;
        _currentVid = vid;
        _currentAudioIdx = null;
        _knownDuration = info.duration || 0;
        _startOffset = 0;

        video.controls = true;

        // Abort previous player event listeners (prevents accumulation across plays)
        if (overlay._playerAC) overlay._playerAC.abort();
        const ac = new AbortController();
        overlay._playerAC = ac;
        const sig = { signal: ac.signal };

        // Hide custom controls — use native browser controls
        const center = bodyEl.querySelector('#vs-player-center');
        const bottom = bodyEl.querySelector('#vs-player-bottom');
        if (center) center.style.display = 'none';
        if (bottom) bottom.style.display = 'none';

        // Auto-hide top bar after 3s
        function _resetHideTimer() {
            clearTimeout(_ctrlHideTimer);
            if (!video.paused) {
                _ctrlHideTimer = setTimeout(() => {
                    overlay.classList.add('vs-ctrl-hidden');
                    _ctrlVisible = false;
                }, 3000);
            }
        }

        function _showControls() {
            overlay.classList.remove('vs-ctrl-hidden');
            _ctrlVisible = true;
            _resetHideTimer();
        }

        // Show controls on open
        overlay.classList.remove('vs-ctrl-hidden');
        _ctrlVisible = true;

        // Mouse move on overlay shows top bar
        overlay.addEventListener('mousemove', () => { _showControls(); }, sig);
        video.addEventListener('play', () => { _resetHideTimer(); }, sig);
        video.addEventListener('pause', () => {
            clearTimeout(_ctrlHideTimer);
            overlay.classList.remove('vs-ctrl-hidden');
            _ctrlVisible = true;
        }, sig);

        title.textContent = info.title || info.filename || '';
        badge.style.display = needsTranscode ? '' : 'none';

        // Backdrop blur background
        if (info.backdrop_ok) {
            overlay.style.backgroundImage = 'url(/api/video-station/backdrop/' + vid + '?token=' + NAS.token + ')';
            overlay.style.backgroundSize = 'cover';
            overlay.style.backgroundPosition = 'center';
            overlay.classList.add('vs-player-has-backdrop');
        } else {
            overlay.style.backgroundImage = '';
            overlay.style.backgroundSize = '';
            overlay.classList.remove('vs-player-has-backdrop');
        }

        // RemotePlayback / Cast button
        const castBtn = bodyEl.querySelector('#vs-cast-btn');
        if (castBtn) {
            if ('remote' in HTMLVideoElement.prototype || video.remote) {
                castBtn.style.display = '';
                castBtn.onclick = () => { video.remote.prompt().catch(() => {}); };
                video.remote.addEventListener('connecting', () => { castBtn.innerHTML = '<i class="fas fa-spinner fa-spin"></i>'; });
                video.remote.addEventListener('connect', () => { castBtn.innerHTML = '<i class="fas fa-tv" style="color:#1db954"></i>'; castBtn.title = t('Casting aktywny'); });
                video.remote.addEventListener('disconnect', () => { castBtn.innerHTML = '<i class="fas fa-tv"></i>'; castBtn.title = t('Cast na TV'); });
            } else {
                castBtn.style.display = 'none';
            }
        }

        // Thumbstrip seek preview
        _initThumbstrip(vid, video, info.duration || 0);

        // Playback speed
        if (speedSel) {
            speedSel.value = '1';
            video.playbackRate = 1;
            speedSel.onchange = () => { video.playbackRate = parseFloat(speedSel.value); };
        }

        // Audio track selector (for transcoded content only)
        const tracks = info.audio_tracks || [];
        if (tracks.length > 1 && needsTranscode) {
            audioSel.innerHTML = tracks.map(t => {
                const label = [t.language, t.title, t.codec, t.channels ? t.channels + 'ch' : ''].filter(Boolean).join(' \u00b7 ') || 'Track ' + t.index;
                return '<option value="' + t.index + '">' + escH(label) + '</option>';
            }).join('');
            audioSel.style.display = '';
            audioSel.onchange = () => {
                _currentAudioIdx = parseInt(audioSel.value);
                _startHls(vid, video.currentTime + _startOffset, _currentAudioIdx);
            };
        } else {
            audioSel.style.display = 'none';
        }

        // Subtitle selector — hidden until tracks are loaded
        if (subSel) { subSel.style.display = 'none'; subSel.innerHTML = ''; }

        const resumePos = (info.position && info.position > 0 && !info.watched) ? info.position : 0;

        async function _doPlay(startSec) {
            if (needsTranscode) {
                await _startHls(vid, startSec, null);
            } else {
                video.src = _buildStreamUrl(vid);
                if (startSec > 0) {
                    video.addEventListener('loadedmetadata', function onMeta() {
                        video.currentTime = startSec;
                        video.removeEventListener('loadedmetadata', onMeta);
                    });
                }
            }
        }

        if (resumePos > 30) {
            const dlg = bodyEl.querySelector('#vs-resume-dialog');
            const txt = bodyEl.querySelector('#vs-resume-text');
            const btnCont = bodyEl.querySelector('#vs-resume-continue');
            const btnRestart = bodyEl.querySelector('#vs-resume-restart');
            if (dlg && txt) {
                video.removeAttribute('autoplay');
                txt.textContent = t('Kontynuować od') + ' ' + formatDuration(resumePos) + '?';
                dlg.style.display = 'flex';
                btnCont.onclick = () => { dlg.style.display = 'none'; _doPlay(resumePos); };
                btnRestart.onclick = () => { dlg.style.display = 'none'; _doPlay(0); };
            } else {
                await _doPlay(resumePos);
            }
        } else {
            await _doPlay(0);
        }

        // Load subtitles (file-based + embedded via HLS) and populate subtitle picker
        _loadSubtitles(vid, video);

        // HLS seeking beyond buffer
        if (needsTranscode) {
            video.onseeking = () => {
                if (!_transcoding || !_hlsInstance) return;
                const buf = video.buffered;
                const t = video.currentTime;
                let inBuffer = false;
                for (let i = 0; i < buf.length; i++) {
                    if (t >= buf.start(i) - 1 && t <= buf.end(i) + 1) { inBuffer = true; break; }
                }
                if (!inBuffer) {
                    const realPos = _startOffset + t;
                    _startHls(vid, realPos, _currentAudioIdx);
                }
            };
        }

        overlay.style.display = 'flex';
        video.focus();
        // Set initial duration on seekbar
        _showControls();

        // save position every 10 seconds
        playerInterval = setInterval(() => savePosition(vid, video), 10000);

        // mark watched at >90%
        video.ontimeupdate = () => {
            const realPos = _transcoding ? (_startOffset + (video.currentTime || 0)) : video.currentTime;
            const totalDur = _transcoding ? _knownDuration : video.duration;
            if (totalDur && realPos / totalDur > 0.9) {
                api('/video-station/watched/' + vid, { method: 'POST', body: { watched: true, position: realPos } });
                video.ontimeupdate = null;
            }
        };

        video.onended = () => {
            const realPos = _transcoding ? _knownDuration : video.duration;
            api('/video-station/watched/' + vid, { method: 'POST', body: { watched: true, position: realPos } });
            closePlayer();
        };

        // Fallback: if raw stream fails, retry with HLS transcode
        video.onerror = () => {
            if (!_transcoding && video.error) {
                _cl('warn', 'Direct stream failed, falling back to HLS transcode', {
                    vid, codec: info.codec, audioCodec: info.audio_codec,
                    errorCode: video.error.code, errorMsg: video.error.message
                });
                _transcoding = true;
                badge.style.display = '';
                _startHls(vid, 0, null);
            }
        };

        // keyboard shortcuts
        overlay._keyHandler = (e) => {
            if (e.key === ' ' || e.code === 'Space') { e.preventDefault(); video.paused ? video.play() : video.pause(); }
            else if (e.key === 'f') { toggleFullscreen(overlay); }
            else if (e.key === 'p') { togglePiP(video); }
            else if (e.key === 'Escape') { closePlayer(); }
            else if (e.key === 'ArrowLeft' || e.key === 'j' || e.key === 'J') { seekPlayer(video, -10); }
            else if (e.key === 'ArrowRight' || e.key === 'l' || e.key === 'L') { seekPlayer(video, 10); }
            else if (e.key === 'k' || e.key === 'K') { video.paused ? video.play() : video.pause(); }
            else if (e.key === 'ArrowUp') { e.preventDefault(); video.volume = Math.min(1, video.volume + 0.1); }
            else if (e.key === 'ArrowDown') { e.preventDefault(); video.volume = Math.max(0, video.volume - 0.1); }
            else if (e.key === 'm') { video.muted = !video.muted; }
        };
        document.addEventListener('keydown', overlay._keyHandler);

        // Stats overlay
        const statsBtn = bodyEl.querySelector('#vs-stats-btn');
        const statsOverlay = bodyEl.querySelector('#vs-stats-overlay');
        if (statsBtn && statsOverlay) {
            statsBtn.onclick = async () => {
                const visible = statsOverlay.style.display !== 'none';
                if (visible) { statsOverlay.style.display = 'none'; return; }
                const enc = await api('/video-station/hls/encoder-info').catch(() => null);
                const isSw = !enc || enc.type !== 'hw';
                const isIhd = enc && enc.type === 'hw' && enc.tooltip && enc.tooltip.includes('iHD');
                const hwTooltip = enc && enc.tooltip ? enc.tooltip
                    : (isIhd ? 'Intel QuickSync (iHD) — Aktywny' : (enc && enc.type === 'hw' ? 'Akceleracja GPU' : ''));
                const swTip = isSw
                    ? '<div class="vs-stats-hw-tip"><i class="fas fa-lightbulb"></i> ' +
                      t('Używasz') + ' <strong>libx264 (CPU)</strong>. ' +
                      t('Zainstaluj sterowniki VAAPI, aby odciążyć procesor i wydłużyć żywotność NucBoxa.') +
                      ' <a class="vs-stats-hw-link" href="#" id="vs-stats-hw-setup">' + t('Jak naprawić?') + '</a></div>'
                    : '';
                statsOverlay.innerHTML =
                    '<div class="vs-stats-row"><span>' + t('Enkoder') + '</span><span>' +
                    escH(enc && enc.label ? enc.label : '—') + '</span></div>' +
                    '<div class="vs-stats-row"><span>' + t('Typ') + '</span>' +
                    '<span title="' + escH(hwTooltip) + '">' +
                    (enc && enc.type === 'hw'
                        ? '<i class="fas fa-bolt" style="color:var(--success)"></i> Intel QuickSync (iHD)'
                        : '<i class="fas fa-' + (enc ? 'exclamation-triangle' : 'microchip') + '" style="color:var(--text-secondary)"></i> CPU (libx264)') +
                    '</span></div>' +
                    '<div class="vs-stats-row"><span>HLS</span><span>' + (_hlsSessionId ? escH(_hlsSessionId.slice(0,8) + '…') : t('Brak sesji')) + '</span></div>' +
                    '<div class="vs-stats-row"><span>' + t('Pozycja') + '</span><span>' + Math.round(video.currentTime) + 's</span></div>' +
                    '<div class="vs-stats-row"><span>' + t('Kodek video') + '</span><span>' + escH(info.codec || '—') + '</span></div>' +
                    '<div class="vs-stats-row"><span>' + t('Kodek audio') + '</span><span>' + escH(info.audio_codec || '—') + '</span></div>' +
                    '<div class="vs-stats-row"><span>' + t('Rozdzielczość') + '</span><span>' + (info.width && info.height ? info.width + '×' + info.height : '—') + '</span></div>' +
                    swTip;
                statsOverlay.style.display = '';

                const setupLink = statsOverlay.querySelector('#vs-stats-hw-setup');
                if (setupLink) {
                    setupLink.onclick = async (e) => {
                        e.preventDefault();
                        statsOverlay.style.display = 'none';
                        const health = await api('/video-station/hw-health').catch(() => null);
                        if (health) _openHwWizard(bodyEl, health);
                    };
                }
            };
        }

        // Touch gestures: double-tap left/right for ±10s seek, vertical swipe for volume (right) / brightness (left)
        let _lastTapTime = 0, _lastTapX = 0;
        let _swipeStartY = 0, _swipeStartX = 0, _swipeSide = null, _swipeBrightness = 1;
        const swipeHint = bodyEl.querySelector('#vs-swipe-hint');
        const seekHintL = bodyEl.querySelector('#vs-seek-hint-left');
        const seekHintR = bodyEl.querySelector('#vs-seek-hint-right');

        function _showSeekHint(side) {
            const el = side === 'left' ? seekHintL : seekHintR;
            if (!el) return;
            el.classList.add('vs-seek-hint-visible');
            clearTimeout(el._ht);
            el._ht = setTimeout(() => el.classList.remove('vs-seek-hint-visible'), 700);
        }

        video.addEventListener('touchstart', (e) => {
            const touch = e.changedTouches[0];
            const now = Date.now();
            const rect = video.getBoundingClientRect();
            const relX = touch.clientX - rect.left;
            const relY = touch.clientY - rect.top;
            _swipeStartX = relX; _swipeStartY = relY;
            _swipeSide = relX < rect.width / 2 ? 'left' : 'right';
            if (now - _lastTapTime < 300 && Math.abs(relX - _lastTapX) < 80) {
                e.preventDefault();
                if (relX < rect.width / 2) { seekPlayer(video, -10); _showSeekHint('left'); }
                else { seekPlayer(video, 10); _showSeekHint('right'); }
                _showControls();
                _lastTapTime = 0;
            } else {
                _lastTapTime = now; _lastTapX = relX;
            }
        }, { passive: false, signal: ac.signal });

        video.addEventListener('touchmove', (e) => {
            const touch = e.changedTouches[0];
            const rect = video.getBoundingClientRect();
            const dy = _swipeStartY - (touch.clientY - rect.top);
            if (Math.abs(dy) < 10) return;
            const ratio = dy / rect.height;
            if (_swipeSide === 'right') {
                video.volume = Math.max(0, Math.min(1, video.volume + ratio * 0.5));
                if (swipeHint) { swipeHint.textContent = t('Głośność') + ': ' + Math.round(video.volume * 100) + '%'; swipeHint.style.display = ''; }
            } else {
                _swipeBrightness = Math.max(0.2, Math.min(2, _swipeBrightness + ratio * 1.0));
                video.style.filter = 'brightness(' + _swipeBrightness + ')';
                if (swipeHint) { swipeHint.textContent = t('Jasność') + ': ' + Math.round(_swipeBrightness * 100) + '%'; swipeHint.style.display = ''; }
            }
            _swipeStartY = touch.clientY - rect.top;
        }, { passive: true, signal: ac.signal });

        video.addEventListener('touchend', () => {
            if (swipeHint) { setTimeout(() => { swipeHint.style.display = 'none'; }, 1200); }
        }, sig);

        // PiP button
        const pipBtn = bodyEl.querySelector('#vs-pip-btn');
        if (pipBtn) {
            pipBtn.style.display = document.pictureInPictureEnabled ? '' : 'none';
            pipBtn.onclick = () => togglePiP(video);
        }

        // Fullscreen button
        const fsBtn = bodyEl.querySelector('#vs-fs-btn');
        if (fsBtn) {
            fsBtn.onclick = () => toggleFullscreen(overlay);
        }

        // Intercept native video fullscreen: redirect to overlay fullscreen
        video.addEventListener('fullscreenchange', function _fsRedirect() {
            if (document.fullscreenElement === video) {
                document.exitFullscreen().then(() => {
                    overlay.requestFullscreen().catch(() => {});
                }).catch(() => {});
            }
        }, sig);

        bodyEl.querySelector('#vs-player-close').onclick = () => closePlayer();
    }

    /**
     * Start HLS transcoding session and attach to video element.
     * Uses hls.js (for Chrome/Firefox) or native HLS (Safari).
     */
    async function _startHls(vid, startSec, audioIdx) {
        const video = bodyEl.querySelector('#vs-player-video');
        if (!video) return;

        // Destroy previous HLS instance
        _destroyHls();

        // Start backend HLS session
        const body = { start: startSec || 0 };
        if (audioIdx != null) body.audio = audioIdx;
        const res = await api('/video-station/hls/' + vid + '/start', { method: 'POST', body });
        if (!res.ok) {
            toast(res.error || t('Błąd transkodowania'), 'error');
            return;
        }
        _hlsSessionId = res.session_id;
        _startOffset = res.start_offset || 0;
        _startHeartbeat(video);

        // Load embedded subtitle tracks from HLS start response
        _loadSubtitleTracks(vid, res.sub_tracks || []);

        const playlistUrl = '/api/video-station/hls/' + _hlsSessionId + '/playlist.m3u8?token=' + NAS.token;

        // Safari supports HLS natively
        if (video.canPlayType('application/vnd.apple.mpegurl')) {
            video.src = playlistUrl;
            video.play().catch(() => {});
            return;
        }

        // Other browsers: use hls.js
        try {
            await _ensureHlsJs();
        } catch (e) {
            toast(t('Nie udało się załadować hls.js'), 'error');
            return;
        }

        if (!Hls.isSupported()) {
            toast(t('Przeglądarka nie wspiera HLS'), 'error');
            return;
        }

        const hls = new Hls({
            xhrSetup: (xhr, url) => {
                // Add auth token to each segment/playlist request
                const sep = url.includes('?') ? '&' : '?';
                xhr.open('GET', url + sep + 'token=' + NAS.token, true);
            },
            maxBufferLength: 60,
            maxMaxBufferLength: 120,
            startPosition: -1,
            liveSyncDurationCount: 3,
            liveMaxLatencyDurationCount: 6,
            fragLoadingTimeOut: 30000,
            fragLoadingMaxRetry: 3,
            fragLoadingRetryDelay: 1000,
            levelLoadingTimeOut: 15000,
        });

        _hlsInstance = hls;

        hls.loadSource(playlistUrl);
        hls.attachMedia(video);

        hls.on(Hls.Events.MANIFEST_PARSED, () => {
            video.play().catch(() => {});
        });

        hls.on(Hls.Events.ERROR, (event, data) => {
            if (data.fatal) {
                if (data.type === Hls.ErrorTypes.NETWORK_ERROR) {
                    // Retry — segment might not be produced yet
                    setTimeout(() => hls.startLoad(), 2000);
                } else if (data.type === Hls.ErrorTypes.MEDIA_ERROR) {
                    hls.recoverMediaError();
                } else {
                    toast(t('Błąd odtwarzania HLS'), 'error');
                }
            }
        });
    }

    function _startHeartbeat(video) {
        if (_heartbeatTimer) clearInterval(_heartbeatTimer);
        _heartbeatTimer = setInterval(() => {
            if (!_hlsSessionId) { clearInterval(_heartbeatTimer); return; }
            const pos = video ? video.currentTime : 0;
            api('/video-station/hls/' + _hlsSessionId + '/heartbeat',
                { method: 'POST', body: { pos } });
        }, 8000);
    }

    function _destroyHls() {
        if (_heartbeatTimer) { clearInterval(_heartbeatTimer); _heartbeatTimer = null; }
        if (_hlsInstance) {
            _hlsInstance.destroy();
            _hlsInstance = null;
        }
        if (_hlsSessionId) {
            api('/video-station/hls/' + _hlsSessionId + '/stop', { method: 'POST' });
            _hlsSessionId = null;
        }
    }

    async function _loadSubtitles(vid, video) {
        video.querySelectorAll('track').forEach(t => t.remove());
        const subSel = bodyEl.querySelector('#vs-sub-select');
        const allTracks = [];

        // Load file-based subtitles
        const data = await api('/video-station/subtitles/' + vid);
        if (data.ok && data.subtitles && data.subtitles.length) {
            data.subtitles.forEach((sub, i) => {
                const track = document.createElement('track');
                track.kind = 'subtitles';
                track.label = sub.language || sub.filename;
                track.srclang = sub.language || 'und';
                track.src = '/api/video-station/subtitle-file/' + vid + '/' + encodeURIComponent(sub.filename) + '?token=' + NAS.token;
                video.appendChild(track);
                allTracks.push({ label: track.label, idx: i, type: 'file' });
            });
        }

        // Populate subtitle picker
        _updateSubSelect(video, allTracks);
    }

    function _loadSubtitleTracks(vid, subTracks) {
        const video = bodyEl && bodyEl.querySelector('#vs-player-video');
        if (!video || !subTracks || !subTracks.length) return;
        video.querySelectorAll('track[data-embedded]').forEach(t => t.remove());
        const existingCount = video.querySelectorAll('track:not([data-embedded])').length;
        subTracks.forEach((sub, i) => {
            const track = document.createElement('track');
            track.kind = 'subtitles';
            track.label = (sub.title || sub.language || ('Track ' + (i + 1)));
            track.srclang = sub.language || 'und';
            track.src = '/api/video-station/embedded-subs/' + vid + '/' + sub.index + '?token=' + NAS.token;
            track.dataset.embedded = '1';
            video.appendChild(track);
        });
        // Rebuild subtitle picker with all tracks
        const allTracks = [];
        const trackEls = video.querySelectorAll('track');
        trackEls.forEach((tr, i) => {
            allTracks.push({ label: tr.label || ('Track ' + (i + 1)), idx: i, type: tr.dataset.embedded ? 'embedded' : 'file' });
        });
        _updateSubSelect(video, allTracks);
    }

    function _updateSubSelect(video, allTracks) {
        const subSel = bodyEl && bodyEl.querySelector('#vs-sub-select');
        if (!subSel) return;
        if (!allTracks.length) {
            subSel.style.display = 'none';
            return;
        }
        const savedLang = localStorage.getItem('vs_sub_lang') || '';
        let html = '<option value="-1">' + escH(t('Napisy: wyłączone')) + '</option>';
        allTracks.forEach((tr, i) => {
            const icon = tr.type === 'embedded' ? '\u{1F4E6} ' : '\u{1F4C4} ';
            html += '<option value="' + i + '">' + icon + escH(tr.label) + '</option>';
        });
        subSel.innerHTML = html;
        subSel.style.display = '';

        // Auto-select matching saved language or first track
        let autoIdx = -1;
        if (savedLang) {
            autoIdx = allTracks.findIndex(tr => tr.label.toLowerCase().includes(savedLang.toLowerCase()));
        }
        if (autoIdx < 0 && allTracks.length > 0) autoIdx = 0;

        subSel.value = String(autoIdx);
        _applySubTrack(video, autoIdx);

        subSel.onchange = () => {
            const idx = parseInt(subSel.value);
            _applySubTrack(video, idx);
            // Remember last selected language
            if (idx >= 0 && allTracks[idx]) {
                localStorage.setItem('vs_sub_lang', allTracks[idx].label);
            }
        };
    }

    function _applySubTrack(video, activeIdx) {
        const tracks = video.textTracks;
        for (let i = 0; i < tracks.length; i++) {
            tracks[i].mode = (i === activeIdx) ? 'showing' : 'hidden';
        }
    }

    function seekPlayer(video, delta) {
        if (_transcoding) {
            // HLS: native seeking works within buffered range
            const realPos = _startOffset + (video.currentTime || 0);
            const newTime = Math.max(0, Math.min(_knownDuration, realPos + delta));
            // If within buffer, use native seek
            const bufferEnd = _startOffset + (video.buffered.length ? video.buffered.end(video.buffered.length - 1) : 0);
            if (newTime >= _startOffset && newTime <= bufferEnd) {
                video.currentTime = newTime - _startOffset;
            } else {
                // Beyond buffer: restart HLS from new position
                _startHls(_currentVid, newTime, _currentAudioIdx);
            }
        } else {
            video.currentTime = Math.max(0, Math.min(video.duration || 0, video.currentTime + delta));
        }
    }

    function savePosition(vid, video) {
        if (!video || video.paused) return;
        const realPos = _transcoding ? (_startOffset + (video.currentTime || 0)) : video.currentTime;
        api('/video-station/watched/' + vid, { method: 'POST', body: { watched: false, position: realPos } });
    }

    function closePlayer() {
        const overlay = bodyEl.querySelector('#vs-player-overlay');
        const video   = bodyEl.querySelector('#vs-player-video');
        if (!overlay) return;

        stopPlayer();
        _destroyHls();
        _transcoding = false;
        _currentVid = null;
        _currentAudioIdx = null;
        _startOffset = 0;
        _knownDuration = 0;
        clearTimeout(_ctrlHideTimer);
        // Abort all addEventListener handlers from openPlayer
        if (overlay._playerAC) { overlay._playerAC.abort(); overlay._playerAC = null; }
        overlay.style.display = 'none';
        overlay.classList.remove('vs-ctrl-hidden');
        if (video) {
            video.pause();
            video.controls = false;
            video.onseeking = null;
            video.ontimeupdate = null;
            video.onended = null;
            video.onerror = null;
            video.onplay = null;
            video.onpause = null;
            video.removeAttribute('src');
            video.load();
        }
        if (overlay._keyHandler) { document.removeEventListener('keydown', overlay._keyHandler); overlay._keyHandler = null; }
        if (overlay._tsCleanup) { overlay._tsCleanup(); overlay._tsCleanup = null; }
        // clear backdrop
        overlay.style.backgroundImage = '';
        overlay.classList.remove('vs-player-has-backdrop');
        _thumbVtt = null; _thumbImg = null; _thumbVid = null;

        // refresh current view to reflect watch state
        _reloadSection();
    }

    function stopPlayer() {
        if (playerInterval) { clearInterval(playerInterval); playerInterval = null; }
    }

    function toggleFullscreen(el) {
        if (!document.fullscreenElement) el.requestFullscreen().catch(() => {});
        else document.exitFullscreen();
    }

    function togglePiP(video) {
        if (!document.pictureInPictureEnabled) return;
        if (document.pictureInPictureElement) {
            document.exitPictureInPicture().catch(() => {});
        } else {
            video.requestPictureInPicture().catch(() => {});
        }
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
'.vs-main{flex:1;display:flex;flex-direction:column;overflow:hidden;min-width:0;min-height:0}',

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
'.vs-content{flex:1;overflow-y:auto;padding:16px;min-height:0}',
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
'.vs-meta{padding:0 10px 4px;font-size:11px;color:var(--text-muted);white-space:nowrap;overflow:hidden;text-overflow:ellipsis}',
'.vs-genre-badges{padding:0 10px 8px;display:flex;gap:4px;flex-wrap:wrap}',
'.vs-genre-badge{font-size:10px;background:rgba(79,140,255,.15);color:rgba(79,140,255,.9);border:1px solid rgba(79,140,255,.2);border-radius:3px;padding:1px 6px;white-space:nowrap}',
'.vs-card-hover-play{position:absolute;inset:0;display:flex;align-items:center;justify-content:center;z-index:4;opacity:0;transition:opacity .18s;background:rgba(0,0,0,.35)}',
'.vs-card:hover .vs-card-hover-play{opacity:1}',
'.vs-card-hover-play i{font-size:32px;color:#fff;filter:drop-shadow(0 2px 8px rgba(0,0,0,.7))}',

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
'.vs-player-overlay{position:fixed;top:0;left:0;width:100vw;height:100vh;background:#000;z-index:10000;display:flex;flex-direction:column;overflow:hidden}',
'.vs-player-overlay:fullscreen{width:100%;height:100%}',
'.vs-player-overlay.vs-player-has-backdrop::before{content:"";position:absolute;inset:0;background:inherit;filter:blur(60px) brightness(0.25);transform:scale(1.1);z-index:0}',
'#vs-player-video{position:relative;z-index:1;width:100%;height:100%;outline:none;object-fit:contain}',
'.vs-player-top{display:flex;align-items:center;justify-content:space-between;padding:10px 16px;z-index:10;background:linear-gradient(to bottom,rgba(0,0,0,.85),transparent);position:absolute;top:0;left:0;right:0}',
'.vs-player-title{color:#fff;font-size:14px;font-weight:500;overflow:hidden;text-overflow:ellipsis;white-space:nowrap;flex:1;min-width:0}',
'.vs-player-close{background:none;border:none;color:#fff;font-size:20px;cursor:pointer;padding:4px 8px;opacity:.7;transition:opacity .15s}',
'.vs-player-close:hover{opacity:1}',
'.vs-player-badge{display:inline-flex;align-items:center;gap:5px;background:rgba(255,165,0,.85);color:#000;font-size:11px;font-weight:600;padding:3px 10px;border-radius:12px;white-space:nowrap}',
'.vs-audio-select{background:rgba(255,255,255,.15);color:#fff;border:1px solid rgba(255,255,255,.3);border-radius:4px;padding:2px 6px;font-size:12px;max-width:220px;cursor:pointer}',
'.vs-audio-select option{background:#222;color:#fff}',
'.vs-sub-select{background:rgba(255,255,255,.15);color:#fff;border:1px solid rgba(255,255,255,.3);border-radius:4px;padding:2px 6px;font-size:12px;max-width:220px;cursor:pointer}',
'.vs-sub-select option{background:#222;color:#fff}',
'.vs-speed-select{background:rgba(255,255,255,.15);color:#fff;border:1px solid rgba(255,255,255,.3);border-radius:4px;padding:2px 6px;font-size:12px;cursor:pointer}',
'.vs-speed-select option{background:#222;color:#fff}',
'.vs-ctx-menu{position:fixed;background:var(--bg-elevated,#2a2a2e);border:1px solid var(--border);border-radius:var(--r-md,6px);padding:4px 0;z-index:9999;min-width:180px;box-shadow:0 8px 24px rgba(0,0,0,.5)}',
'.vs-ctx-item{padding:8px 14px;cursor:pointer;font-size:13px;color:var(--text-primary,#fff);display:flex;align-items:center;gap:8px;white-space:nowrap}',
'.vs-ctx-item:hover{background:var(--bg-hover,rgba(255,255,255,.08))}',
'.vs-ctx-item i{width:16px;text-align:center;opacity:.7}',
'.vs-ctx-danger{color:var(--danger,#f87171)}',
'.vs-ctx-danger:hover{background:rgba(248,113,113,.12)}',

/* PiP, fullscreen & cast buttons */
'.vs-pip-btn{background:none;border:none;color:#fff;font-size:15px;cursor:pointer;padding:4px 8px;opacity:.7;transition:opacity .15s}',
'.vs-pip-btn:hover{opacity:1}',
'.vs-fs-btn{background:none;border:none;color:#fff;font-size:15px;cursor:pointer;padding:4px 8px;opacity:.7;transition:opacity .15s}',
'.vs-fs-btn:hover{opacity:1}',
'.vs-cast-btn{background:none;border:none;color:#fff;font-size:15px;cursor:pointer;padding:4px 8px;opacity:.7;transition:opacity .15s}',
'.vs-cast-btn:hover{opacity:1;color:#1db954}',

/* Thumbstrip seek preview */
'.vs-thumbstrip-preview{position:absolute;display:flex;flex-direction:column;align-items:center;gap:4px;pointer-events:none;z-index:15;bottom:70px}',
'.vs-thumbstrip-preview canvas{border-radius:4px;border:2px solid rgba(255,255,255,.3);box-shadow:0 4px 16px rgba(0,0,0,.8)}',
'.vs-thumbstrip-time{color:#fff;font-size:11px;font-weight:600;background:rgba(0,0,0,.7);padding:2px 8px;border-radius:4px;font-variant-numeric:tabular-nums}',

/* Resume dialog */
'.vs-resume-dialog{position:absolute;inset:0;background:rgba(0,0,0,.75);display:flex;align-items:center;justify-content:center;z-index:20}',
'.vs-resume-box{background:var(--bg-elevated,#2a2a2e);border-radius:12px;padding:28px 36px;text-align:center;box-shadow:0 8px 32px rgba(0,0,0,.6)}',
'.vs-resume-text{color:#fff;font-size:16px;margin-bottom:20px;font-weight:500}',
'.vs-resume-btns{display:flex;gap:12px;justify-content:center}',
'.vs-resume-btn{display:inline-flex;align-items:center;gap:8px;padding:10px 24px;border:none;border-radius:8px;font-size:14px;font-weight:600;cursor:pointer;transition:filter .15s;background:var(--accent);color:#fff}',
'.vs-resume-btn:hover{filter:brightness(1.15)}',
'.vs-resume-secondary{background:rgba(255,255,255,.15);color:#fff}',
'.vs-resume-secondary:hover{background:rgba(255,255,255,.25)}',

/* Continue Watching */
'.vs-section-header{font-size:15px;font-weight:600;color:var(--text-primary);padding:0 0 10px;display:flex;align-items:center;gap:8px}',
'.vs-section-header i{color:var(--accent);font-size:14px}',
'.vs-horiz-scroll{display:flex;gap:12px;overflow-x:auto;padding:0 0 16px;margin-bottom:16px;scrollbar-width:thin}',
'.vs-cw-card{min-width:200px;max-width:200px;cursor:pointer;border-radius:var(--r-md);overflow:hidden;background:var(--bg-secondary);border:1px solid var(--border);flex-shrink:0;transition:transform .15s,box-shadow .15s}',
'.vs-cw-card:hover{transform:translateY(-2px);box-shadow:0 4px 16px rgba(0,0,0,.25)}',
'.vs-cw-thumb{position:relative;aspect-ratio:16/9;background:#111;overflow:hidden;display:flex;align-items:center;justify-content:center}',
'.vs-cw-thumb img{width:100%;height:100%;object-fit:cover;position:absolute;inset:0;z-index:1}',
'.vs-cw-play{position:absolute;inset:0;display:flex;align-items:center;justify-content:center;z-index:3;opacity:0;transition:opacity .15s;background:rgba(0,0,0,.4)}',
'.vs-cw-card:hover .vs-cw-play{opacity:1}',
'.vs-cw-play i{font-size:28px;color:#fff;text-shadow:0 2px 8px rgba(0,0,0,.5)}',
'.vs-cw-remaining{position:absolute;bottom:8px;left:6px;background:rgba(0,0,0,.8);color:#fff;font-size:10px;padding:2px 6px;border-radius:3px;z-index:2}',

/* Info modal */
'.vs-info-modal{position:fixed;top:0;left:0;width:100vw;height:100vh;background:rgba(0,0,0,.85);z-index:9500;display:flex;align-items:center;justify-content:center}',
'.vs-info-content{width:90%;max-width:900px;max-height:85vh;overflow-y:auto;border-radius:12px;overflow:hidden;position:relative}',
'.vs-info-backdrop{position:relative;min-height:400px;background-size:cover;background-position:center;background-color:#1a1a2e}',
'.vs-info-gradient{position:absolute;inset:0;background:linear-gradient(to top,rgba(0,0,0,.95) 0%,rgba(0,0,0,.7) 40%,rgba(0,0,0,.4) 100%)}',
'.vs-info-close{position:absolute;top:12px;right:12px;background:rgba(0,0,0,.5);border:none;color:#fff;font-size:18px;cursor:pointer;z-index:2;padding:6px 10px;border-radius:50%;transition:background .15s}',
'.vs-info-close:hover{background:rgba(255,255,255,.2)}',
'.vs-info-body{position:relative;z-index:1;display:flex;gap:24px;padding:40px 32px 32px;align-items:flex-end}',
'.vs-info-poster{width:160px;min-width:160px;border-radius:8px;box-shadow:0 4px 20px rgba(0,0,0,.5);object-fit:cover}',
'.vs-info-details{flex:1;min-width:0}',
'.vs-info-title{margin:0 0 8px;font-size:22px;font-weight:700;color:#fff;line-height:1.2}',
'.vs-info-meta{display:flex;align-items:center;gap:6px;font-size:13px;color:rgba(255,255,255,.8);margin-bottom:8px;flex-wrap:wrap}',
'.vs-info-dot{opacity:.4}',
'.vs-info-genres{font-size:12px;color:var(--accent);margin-bottom:10px}',
'.vs-info-overview{font-size:13px;color:rgba(255,255,255,.75);line-height:1.6;margin-bottom:12px;max-height:120px;overflow-y:auto}',
'.vs-info-credit{font-size:12px;color:rgba(255,255,255,.6);margin-bottom:4px}',
'.vs-info-label{color:rgba(255,255,255,.4)}',
'.vs-info-tech{display:flex;gap:8px;flex-wrap:wrap;margin-top:10px}',
'.vs-info-tech span{font-size:11px;background:rgba(255,255,255,.1);color:rgba(255,255,255,.6);padding:2px 8px;border-radius:4px}',
'.vs-info-tc{background:rgba(255,165,0,.2)!important;color:rgba(255,165,0,.9)!important}',
'.vs-info-play{display:inline-flex;align-items:center;gap:8px;background:var(--accent);color:#fff;border:none;padding:10px 24px;border-radius:8px;font-size:14px;font-weight:600;cursor:pointer;margin-top:14px;transition:filter .15s}',
'.vs-info-play:hover{filter:brightness(1.15)}',
'.vs-info-play i{font-size:13px}',

/* multi-select */
'.vs-card.vs-selected{outline:2px solid var(--accent);outline-offset:-2px;border-radius:var(--r-md)}',
'.vs-checkbox{position:absolute;top:6px;left:6px;z-index:5;font-size:18px;color:var(--text-muted);cursor:pointer;opacity:.85;text-shadow:0 1px 3px rgba(0,0,0,.5)}',
'.vs-checkbox.checked{color:var(--accent);opacity:1}',
'.vs-card{position:relative}',
'.vs-select-mode .vs-card{cursor:pointer}',
'#vs-select-toggle.active{background:var(--accent);color:#fff}',

/* batch bar */
'.vs-batch-bar{display:flex;align-items:center;gap:8px;padding:6px 14px;background:var(--bg-secondary);border-bottom:1px solid var(--border);flex-wrap:wrap}',
'.vs-batch-count{font-size:13px;color:var(--text-secondary);margin-right:auto}',
'.vs-batch-danger{color:var(--danger)!important}',

/* nav badge */
'.vs-nav-badge{margin-left:auto;font-size:11px;background:var(--accent);color:#fff;padding:1px 7px;border-radius:10px;min-width:18px;text-align:center}',

/* password modal */
'.vs-modal-overlay{position:absolute;inset:0;background:rgba(0,0,0,.55);display:flex;align-items:center;justify-content:center;z-index:90}',
'.vs-modal-box{background:var(--bg-primary);border:1px solid var(--border);border-radius:var(--r-lg);padding:24px;width:340px;max-width:90%}',
'.vs-modal-box h3{margin:0 0 8px;font-size:15px;color:var(--text-primary)}',
    ].join('\n'); }
};
