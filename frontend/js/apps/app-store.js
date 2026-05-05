/* ═══════════════════════════════════════════════════════════
   EthOS — App Store (Package Center)
   ═══════════════════════════════════════════════════════════ */

window.AppRegistry = window.AppRegistry || {};
const AppRegistry = window.AppRegistry;

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
        otaUpdates: [],      // [{id, name, backend_changed, frontend_changed}, ...]
        otaChecking: false,
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
      <div class="pm-toolbar-actions">
        <button class="pm-ota-btn" id="pm-btn-ota-check" title="${t('Sprawdź aktualizacje aplikacji')}"><i class="fas fa-sync-alt"></i> ${t('Sprawdź aktualizacje')}</button>
        <button class="pm-ota-btn pm-ota-update-all" id="pm-btn-ota-update" style="display:none" title="${t('Zaktualizuj wszystkie')}"><i class="fas fa-cloud-download-alt"></i> ${t('Aktualizuj wszystkie')}</button>
        <button class="pm-ota-btn pm-src-btn" id="pm-btn-src-config" title="${t('Źródło aktualizacji')}"><i class="fas fa-cog"></i></button>
      </div>
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
        // Mark apps with OTA updates
        const otaIds = new Set(S.otaUpdates.map(u => u.id));
        all.forEach(app => { if (otaIds.has(app.id)) app.update_available = true; });
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

    function _anyBusy() {
        return Object.values(S.progressMap).some(p => p.status === 'running' || p.status === 'finishing');
    }

    function _ringSvg(pct, done) {
        const r = 13, c = 2 * Math.PI * r;
        const offset = c - (c * Math.min(pct, 100) / 100);
        // Counter-rotate text 90° to cancel the SVG's -90° rotation
        let inner;
        if (done === 'error') {
            inner = `<text x="16" y="17" transform="rotate(90,16,16)" class="pm-ring-icon pm-ring-icon-error">✗</text>`;
        } else if (done) {
            inner = `<text x="16" y="17" transform="rotate(90,16,16)" class="pm-ring-icon">✓</text>`;
        } else {
            inner = `<text x="16" y="17" transform="rotate(90,16,16)" class="pm-ring-pct">${Math.round(pct)}</text>`;
        }
        const fillClass = done === 'error' ? 'pm-ring-fill pm-ring-fill-error' : (done ? 'pm-ring-fill pm-ring-fill-done' : 'pm-ring-fill');
        return `<svg class="pm-ring-svg" viewBox="0 0 32 32"><circle class="pm-ring-bg" cx="16" cy="16" r="${r}"/><circle class="${fillClass}" cx="16" cy="16" r="${r}" stroke-dasharray="${c.toFixed(1)}" stroke-dashoffset="${offset.toFixed(1)}"/>${inner}</svg>`;
    }

    function renderCard(app) {
        const prog = S.progressMap[app.id];
        const isRunning = prog && prog.status === 'running';
        const isFinishing = prog && prog.status === 'finishing';
        const isErrorStage = prog && prog.stage === 'error';
        const busy = _anyBusy();
        const dis = busy && !isRunning && !isFinishing ? ' disabled' : '';

        let actionHtml = '';
        if (app.core) {
            actionHtml = `<span class="pm-badge-core"><i class="fas fa-lock"></i> Core</span>`;
        } else if (isFinishing) {
            actionHtml = `<div class="pm-ring-wrap">${_ringSvg(100, isErrorStage ? 'error' : true)}</div>`;
        } else if (isRunning) {
            const pct = prog.percent || 0;
            actionHtml = `<div class="pm-ring-wrap">${_ringSvg(pct, false)}</div>`;
        } else if (app.update_available) {
            actionHtml = `<button class="pm-btn-update"${dis} data-id="${app.id}" title="${t('Aktualizuj')}"><i class="fas fa-sync-alt"></i></button>
              <button class="pm-btn-uninstall"${dis} data-id="${app.id}" title="${t('Odinstaluj')}"><i class="fas fa-trash"></i></button>`;
        } else if (app.installed) {
            actionHtml = `<button class="pm-btn-uninstall"${dis} data-id="${app.id}" title="${t('Odinstaluj')}"><i class="fas fa-trash"></i></button>`;
        } else {
            actionHtml = `<button class="pm-btn-install"${dis} data-id="${app.id}" title="${t('Instaluj')}"><i class="fas fa-download"></i></button>`;
        }

        const depsHtml = app.apt_deps?.length || app.pip_deps?.length
            ? `<div class="pm-card-deps">${[...(app.apt_deps||[]), ...(app.pip_deps||[])].slice(0,3).join(', ')}${([...(app.apt_deps||[]), ...(app.pip_deps||[])].length > 3) ? '…' : ''}</div>`
            : '';

        return `<div class="pm-card ${app.installed || app.core ? 'pm-card-installed' : ''} ${app.core ? 'pm-card-core' : ''} ${isRunning ? 'pm-card-busy' : ''}" data-id="${app.id}">
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
            actionBtn = `<span class="pm-badge-progress"><i class="fas fa-circle-notch fa-spin"></i> ${t('Instalowanie…')}</span>`;
        } else if (isInst) {
            const dis = _anyBusy() ? ' disabled' : '';
            actionBtn = `<button class="pm-btn-uninstall"${dis} style="width:auto;padding:5px 12px" onclick="document.getElementById('pm-detail-overlay').style.display='none'; window._pmUninstall('${app.id}')"><i class="fas fa-trash"></i> ${t('Odinstaluj')}</button>`;
            if (app.update_available) {
                actionBtn = `<button class="pm-btn-update"${dis} style="width:auto;padding:5px 12px" onclick="document.getElementById('pm-detail-overlay').style.display='none'; window._pmUpdate('${app.id}')"><i class="fas fa-sync-alt"></i> ${t('Aktualizuj do')} v${escHtml(app.version)}</button> ` + actionBtn;
            }
        } else {
            const dis = _anyBusy() ? ' disabled' : '';
            actionBtn = `<button class="pm-btn-primary"${dis} onclick="document.getElementById('pm-detail-overlay').style.display='none'; window._pmInstall('${app.id}')">${t('Zainstaluj')}</button>`;
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
        if (_anyBusy()) return;
        if (S.progressMap[appId]?.status === 'running') return;
        S.progressMap[appId] = { stage: 'start', percent: 5, message: t('Uruchamianie…'), status: 'running', _started: Date.now() };
        render();
        try {
            const data = await api('/app-manager/' + appId + '/install', { method: 'POST' });
            if (data.error) { toast(data.error, 'error'); delete S.progressMap[appId]; render(); }
        } catch (e) {
            toast(t('Błąd połączenia z serwerem'), 'error');
            delete S.progressMap[appId];
            render();
        }
    }

    async function uninstallApp(appId) {
        if (_anyBusy()) return;
        const app = [...S.catalog, ...S.core].find(a => a.id === appId);
        const nm = app ? app.name : appId;
        if (!await confirmDialog(t('Odinstalować') + ' ' + nm + '?')) return;
        S.progressMap[appId] = { stage: 'start', percent: 5, message: t('Odinstalowywanie…'), status: 'running', _started: Date.now() };
        render();
        try {
            const data = await api('/app-manager/' + appId + '/uninstall', { method: 'POST' });
            if (data.error) { toast(data.error, 'error'); delete S.progressMap[appId]; render(); }
        } catch (e) {
            toast(t('Błąd połączenia z serwerem'), 'error');
            delete S.progressMap[appId];
            render();
        }
    }

    async function updateApp(appId) {
        if (_anyBusy()) return;
        S.progressMap[appId] = { stage: 'start', percent: 5, message: t('Aktualizowanie…'), status: 'running', _started: Date.now() };
        render();
        try {
            const data = await api('/app-manager/' + appId + '/update', { method: 'POST' });
            if (data.error) { toast(data.error, 'error'); delete S.progressMap[appId]; render(); }
        } catch (e) {
            toast(t('Błąd połączenia z serwerem'), 'error');
            delete S.progressMap[appId];
            render();
        }
    }

    async function checkOtaUpdates() {
        if (S.otaChecking || _anyBusy()) return;
        S.otaChecking = true;
        const btn = $('#pm-btn-ota-check');
        if (btn) { btn.disabled = true; btn.innerHTML = '<i class="fas fa-spinner fa-spin"></i> ' + t('Sprawdzanie…'); }
        try {
            const data = await api('/app-manager/check-app-updates', { method: 'POST' });
            if (data.error) { toast(data.error, 'error'); return; }
            S.otaUpdates = data.updates || [];
            const updBtn = $('#pm-btn-ota-update');
            const src = data.repo ? `GitHub (${data.repo})` : 'GitHub';
            if (S.otaUpdates.length) {
                toast(t('{n} aktualizacji dostępnych', { n: S.otaUpdates.length }) + ` [${src}]`, 'info');
                if (updBtn) updBtn.style.display = '';
            } else {
                toast(t('Wszystkie aplikacje aktualne') + ` [${src}]`, 'success');
                if (updBtn) updBtn.style.display = 'none';
            }
            applyFilter();
        } catch (e) {
            toast(t('Błąd sprawdzania aktualizacji'), 'error');
        } finally {
            S.otaChecking = false;
            if (btn) { btn.disabled = false; btn.innerHTML = '<i class="fas fa-sync-alt"></i> ' + t('Sprawdź aktualizacje'); }
        }
    }

    async function updateAllApps() {
        if (_anyBusy() || !S.otaUpdates.length) return;
        const ids = S.otaUpdates.map(u => u.id);
        if (!await confirmDialog(t('Zaktualizować {n} aplikacji z serwera?', { n: ids.length }))) return;
        // Mark all as queued — watchdog must not kill apps waiting their turn
        ids.forEach(id => {
            S.progressMap[id] = { stage: 'start', percent: 2, message: t('Oczekiwanie…'), status: 'running', _started: Date.now(), _lastUpdate: Date.now(), _queued: true };
        });
        render();
        const data = await api('/app-manager/update-apps', { method: 'POST', body: { app_ids: ids } });
        if (data.error) {
            toast(data.error, 'error');
            ids.forEach(id => delete S.progressMap[id]);
            render();
        }
    }

    /* ── SocketIO progress ── */
    function onProgress(ev) {
        const { app_id, stage, percent, message, status, updated, failed } = ev;

        // Per-app progress event from batch update (stage='updating' with app_id)
        if (app_id && stage === 'updating') {
            const prev = S.progressMap[app_id];
            S.progressMap[app_id] = {
                stage, percent, message, status: 'running',
                _started: prev?._started || Date.now(),
                _lastUpdate: Date.now(),
                _queued: false,
            };
            render();
            return;
        }

        // Batch OTA update events without app_id — global start/sync
        if (!app_id && stage === 'updating') {
            return;
        }
        if (!app_id && (status === 'done' || status === 'error') && (updated || failed)) {
            // Batch complete — clear progress for all OTA apps
            (updated || []).forEach(id => {
                S.progressMap[id] = { stage: 'done', percent: 100,
                    message: '<i class="fas fa-check"></i> ' + t('Zaktualizowano'),
                    status: 'finishing' };
            });
            (failed || []).forEach(id => {
                S.progressMap[id] = { stage: 'error', percent: 100,
                    message: '<i class="fas fa-exclamation-triangle"></i> ' + t('Błąd'),
                    status: 'finishing' };
            });
            S.otaUpdates = [];
            const updBtn = body.querySelector('#pm-btn-ota-update');
            if (updBtn) updBtn.style.display = 'none';
            render();
            setTimeout(async () => {
                await loadCatalog();
                (updated || []).concat(failed || []).forEach(id => delete S.progressMap[id]);
                render();
                try {
                    NAS.apps = await api('/apps');
                    renderDesktopIcons();
                    renderMenuGrid();
                } catch (e) {}
            }, 1500);
            return;
        }
        if (!app_id && stage === 'start') {
            // Batch starting — nothing to do, per-app progress already set
            return;
        }

        if (!app_id) return;

        if (status === 'done' || status === 'error') {
            // Show brief success/error state before clearing
            const started = S.progressMap[app_id]?._started || Date.now();
            const label = status === 'done'
                ? '<i class="fas fa-check"></i> ' + (message || t('Gotowe'))
                : '<i class="fas fa-exclamation-triangle"></i> ' + (message || t('Błąd'));
            S.progressMap[app_id] = { stage, percent: 100, message: label, status: 'finishing' };
            render();

            // Enforce minimum visible time so user sees feedback
            const elapsed = Date.now() - started;
            const minTime = 3000;
            const delay = Math.max(minTime - elapsed, 1500);

            setTimeout(async () => {
                // Reload catalog FIRST (while finishing state still visible)
                await loadCatalog();
                // Now catalog has updated installed state — safe to clear progress
                delete S.progressMap[app_id];
                render();
                if (status === 'done') {
                    try {
                        NAS.apps = await api('/apps');
                        renderDesktopIcons();
                        renderMenuGrid();
                    } catch (e) {}
                }
            }, delay);
        } else {
            S.progressMap[app_id] = { stage, percent, message, status, _started: S.progressMap[app_id]?._started || Date.now(), _lastUpdate: Date.now(), _queued: false };
            render();
        }
    }

    // Stale progress watchdog — auto-clear installs stuck for >90s with no update.
    // Queued batch apps (_queued flag) are exempt until they receive their first event.
    const _staleTimer = setInterval(() => {
        const now = Date.now();
        let changed = false;
        for (const [id, p] of Object.entries(S.progressMap)) {
            if (p.status !== 'running') continue;
            if (p._queued) continue;  // batch-queued: exempt from stale check
            const lastUpdate = p._lastUpdate || p._started || now;
            if (now - lastUpdate > 90000) {
                S.progressMap[id] = { stage: 'error', percent: 100,
                    message: '<i class="fas fa-exclamation-triangle"></i> ' + t('Brak odpowiedzi — sprawdź logi'),
                    status: 'finishing' };
                changed = true;
                setTimeout(() => { delete S.progressMap[id]; render(); }, 5000);
            }
        }
        if (changed) render();
    }, 10000);

    // Reconnect recovery — restore running task state from backend
    async function _recoverRunningTasks() {
        try {
            const data = await api('/app-manager/running-tasks');
            if (!data.tasks?.length) return;
            let changed = false;
            for (const task of data.tasks) {
                const aid = task.app_id;
                if (!aid) continue;
                if (!S.progressMap[aid]) {
                    S.progressMap[aid] = {
                        stage: task.stage || 'running',
                        percent: task.percent || 50,
                        message: task.message || t('W toku…'),
                        status: 'running',
                        _started: Date.now(),
                        _lastUpdate: Date.now(),
                    };
                    changed = true;
                }
            }
            if (changed) render();
        } catch (e) { /* ignore */ }
    }

    if (NAS.socket) {
        NAS.socket.on('app_manager_progress', onProgress);
        NAS.socket.on('connect', _recoverRunningTasks);
        body.closest('.window')?.addEventListener('window-close', () => {
            NAS.socket.off('app_manager_progress', onProgress);
            NAS.socket.off('connect', _recoverRunningTasks);
            clearInterval(_staleTimer);
        });
    }
    // Also recover on open in case tasks were running before app was opened
    _recoverRunningTasks();

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

    $('#pm-btn-ota-check')?.addEventListener('click', () => checkOtaUpdates());
    $('#pm-btn-ota-update')?.addEventListener('click', () => updateAllApps());
    $('#pm-btn-src-config')?.addEventListener('click', () => showSourceConfig());

    async function showSourceConfig() {
        const srcRes = await api('/app-manager/catalog-sources');
        if (srcRes.error) { toast(srcRes.error, 'error'); return; }
        const sources = srcRes.sources || [];

        const overlay = $('#pm-detail-overlay');
        overlay.style.display = 'flex';

        function renderSources() {
            const listHtml = sources.map((s, i) => `
              <div class="pm-src-item" data-idx="${i}">
                <div class="pm-src-item-left">
                  <label class="pm-src-toggle">
                    <input type="checkbox" ${s.enabled ? 'checked' : ''} data-action="toggle" data-idx="${i}">
                    <span class="pm-src-slider"></span>
                  </label>
                  <i class="${s.type === 'github' ? 'fab fa-github' : 'fas fa-globe'}" style="font-size:16px;opacity:0.7"></i>
                  <div>
                    <div class="pm-src-name">${escHtml(s.name)}</div>
                    <div class="pm-src-url">${escHtml(s.type === 'github' ? s.repo : s.url)}</div>
                  </div>
                </div>
                <button class="pm-src-del" data-action="delete" data-idx="${i}" title="${t('Usuń')}"><i class="fas fa-trash"></i></button>
              </div>`).join('');

            overlay.innerHTML = `
            <div class="pm-detail-card" style="max-width:560px">
              <div class="pm-detail-header">
                <span class="pm-detail-name"><i class="fas fa-layer-group"></i> ${t('Źródła katalogu aplikacji')}</span>
                <button class="pm-detail-close" id="pm-src-close"><i class="fas fa-times"></i></button>
              </div>
              <div class="pm-detail-body" style="padding:20px">
                <div class="pm-src-list">${listHtml || `<div class="pm-src-empty">${t('Brak źródeł')}</div>`}</div>
                <div class="pm-src-add-row">
                  <button class="pm-ota-btn" id="pm-src-add"><i class="fas fa-plus"></i> ${t('Dodaj źródło')}</button>
                </div>

              </div>
            </div>`;

            overlay.querySelector('#pm-src-close').onclick = () => { overlay.style.display = 'none'; };
            overlay.querySelector('#pm-src-add').onclick = () => showAddForm();

            overlay.querySelectorAll('[data-action="toggle"]').forEach(cb => {
                cb.onchange = async () => {
                    const idx = parseInt(cb.dataset.idx);
                    const s = sources[idx];
                    s.enabled = cb.checked;
                    await api(`/app-manager/catalog-sources/${encodeURIComponent(s.id)}`, {
                        method: 'PUT', body: { enabled: s.enabled }
                    });
                };
            });

            overlay.querySelectorAll('[data-action="delete"]').forEach(btn => {
                btn.onclick = async () => {
                    const idx = parseInt(btn.dataset.idx);
                    const s = sources[idx];
                    if (sources.length <= 1) { toast(t('Musi pozostać co najmniej jedno źródło'), 'warning'); return; }
                    const res = await api(`/app-manager/catalog-sources/${encodeURIComponent(s.id)}`, { method: 'DELETE' });
                    if (res.error) { toast(res.error, 'error'); return; }
                    sources.splice(idx, 1);
                    renderSources();
                    toast(t('Źródło usunięte'), 'success');
                };
            });


        }

        function showAddForm() {
            overlay.innerHTML = `
            <div class="pm-detail-card" style="max-width:480px">
              <div class="pm-detail-header">
                <span class="pm-detail-name"><i class="fas fa-plus-circle"></i> ${t('Dodaj źródło katalogu')}</span>
                <button class="pm-detail-close" id="pm-src-back"><i class="fas fa-arrow-left"></i></button>
              </div>
              <div class="pm-detail-body" style="padding:20px">
                <div style="margin-bottom:14px">
                  <label class="pm-src-label">${t('Nazwa')}</label>
                  <input type="text" id="pm-src-new-name" class="pm-search" style="width:100%" placeholder="${t('np. Moje aplikacje')}">
                </div>
                <div style="margin-bottom:14px">
                  <label class="pm-src-label">${t('Typ')}</label>
                  <div style="display:flex;gap:12px;margin-top:6px">
                    <label class="pm-src-radio"><input type="radio" name="pm-src-type" value="github" checked> <i class="fab fa-github"></i> GitHub</label>
                    <label class="pm-src-radio"><input type="radio" name="pm-src-type" value="url"> <i class="fas fa-globe"></i> URL</label>
                  </div>
                </div>
                <div id="pm-src-github-field" style="margin-bottom:14px">
                  <label class="pm-src-label">${t('Repozytorium (owner/repo)')}</label>
                  <input type="text" id="pm-src-new-repo" class="pm-search" style="width:100%" placeholder="owner/repo">
                </div>
                <div id="pm-src-url-field" style="margin-bottom:14px;display:none">
                  <label class="pm-src-label">${t('URL katalogu')}</label>
                  <input type="text" id="pm-src-new-url" class="pm-search" style="width:100%" placeholder="https://example.com/apps/catalog.json">
                </div>
                <button class="pm-install-btn" id="pm-src-save" style="width:100%"><i class="fas fa-save"></i> ${t('Dodaj')}</button>
              </div>
            </div>`;

            overlay.querySelector('#pm-src-back').onclick = () => renderSources();

            overlay.querySelectorAll('input[name="pm-src-type"]').forEach(r => {
                r.onchange = () => {
                    const isGh = r.value === 'github';
                    overlay.querySelector('#pm-src-github-field').style.display = isGh ? '' : 'none';
                    overlay.querySelector('#pm-src-url-field').style.display = isGh ? 'none' : '';
                };
            });

            overlay.querySelector('#pm-src-save').onclick = async () => {
                const srcType = overlay.querySelector('input[name="pm-src-type"]:checked')?.value || 'github';
                const name = overlay.querySelector('#pm-src-new-name')?.value?.trim();
                if (!name) { toast(t('Podaj nazwę źródła'), 'warning'); return; }
                const body = { type: srcType, name };
                if (srcType === 'github') {
                    body.repo = overlay.querySelector('#pm-src-new-repo')?.value?.trim();
                    if (!body.repo || !body.repo.includes('/')) { toast(t('Format: owner/repo'), 'warning'); return; }
                } else {
                    body.url = overlay.querySelector('#pm-src-new-url')?.value?.trim();
                    if (!body.url) { toast(t('Podaj URL'), 'warning'); return; }
                }
                const res = await api('/app-manager/catalog-sources', { method: 'POST', body });
                if (res.error) { toast(res.error, 'error'); return; }
                sources.push(res.source);
                toast(t('Źródło dodane'), 'success');
                renderSources();
            };
        }

        renderSources();
    }

    loadCatalog();
}


// ═══════════════════════════════════════════════════════════
//  REMOTE LOG (Zdalne logi)
// ═══════════════════════════════════════════════════════════
