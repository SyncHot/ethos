/* ═══════════════════════════════════════════════════════════
   EthOS — Builder  (Release & Image Builder)
   Build release packages and bootable system images.
   ═══════════════════════════════════════════════════════════ */

AppRegistry['builder'] = function (appDef) {
    const _cl = (level, msg, details) => typeof NAS !== 'undefined' && NAS.logClient
        ? NAS.logClient('builder', level, msg, details) : console.log('[builder]', msg, details || '');

    createWindow('builder', {
        title: 'Builder',
        icon: appDef.icon,
        iconColor: appDef.color,
        width: 860,
        height: 620,
        onRender: (body) => renderBuilderApp(body),
    });
};

function renderBuilderApp(body) {
    const state = { info: null, building: false, tab: 'release' };

    body.innerHTML = `
    <style>
        .bl-wrap { display:flex; height:100%; }
        .bl-sidebar { width:180px; min-width:180px; background:var(--bg-secondary,#0f172a); border-right:1px solid var(--border); display:flex; flex-direction:column; padding:8px 0; flex-shrink:0; }
        .bl-nav { padding:10px 18px; cursor:pointer; display:flex; align-items:center; gap:10px; font-size:13px; color:var(--text-secondary,#94a3b8); transition:.15s; border-left:3px solid transparent; }
        .bl-nav:hover { background:var(--bg-hover,rgba(255,255,255,.04)); color:var(--text-primary,#e2e8f0); }
        .bl-nav.active { background:var(--bg-hover,rgba(255,255,255,.06)); color:var(--accent); border-left-color:var(--accent); font-weight:600; }
        .bl-nav i { width:16px; text-align:center; font-size:12px; }
        .bl-body { flex:1; overflow-y:auto; padding:16px; }

        .bl-section { background:var(--bg-card, var(--bg-secondary)); border-radius:10px; padding:16px; margin-bottom:14px; }
        .bl-section-title { font-weight:600; font-size:13px; margin-bottom:10px; display:flex; align-items:center; gap:8px; color:var(--text-primary); }
        .bl-section-title i { width:18px; text-align:center; }

        .bl-row { display:flex; gap:10px; align-items:center; margin-bottom:10px; }
        .bl-row label { min-width:120px; font-size:12px; color:var(--text-muted); }
        .bl-input { flex:1; background:var(--bg-primary); border:1px solid var(--border); border-radius:6px; padding:7px 10px; color:var(--text-primary); font-size:13px; }
        .bl-select { background:var(--bg-primary); border:1px solid var(--border); border-radius:6px; padding:7px 10px; color:var(--text-primary); font-size:13px; }
        .bl-textarea { flex:1; background:var(--bg-primary); border:1px solid var(--border); border-radius:6px; padding:7px 10px; color:var(--text-primary); font-size:12px; font-family:monospace; min-height:60px; resize:vertical; }

        .bl-btn { background:var(--accent); color:#fff; border:none; border-radius:6px; padding:8px 16px; cursor:pointer; font-size:13px; display:inline-flex; align-items:center; gap:6px; }
        .bl-btn:hover { filter:brightness(1.1); }
        .bl-btn:disabled { opacity:.5; cursor:not-allowed; filter:none; }
        .bl-btn-danger { background:#ef4444; }
        .bl-btn-green { background:#10b981; }
        .bl-btn-outline { background:transparent; border:1px solid var(--border); color:var(--text-secondary); }
        .bl-btn-outline:hover { border-color:var(--accent); color:var(--accent); }
        .bl-btn-sm { padding:5px 10px; font-size:12px; }
        .bl-btn-cancel { background:#f59e0b; }

        .bl-ver { display:inline-flex; align-items:center; gap:6px; background:var(--accent); color:#fff; padding:4px 12px; border-radius:20px; font-weight:600; font-size:13px; }

        .bl-progress-wrap { display:none; margin-top:14px; }
        .bl-progress-wrap.active { display:block; }
        .bl-progress-outer { height:22px; background:var(--bg-primary); border-radius:11px; overflow:hidden; position:relative; }
        .bl-progress-inner { height:100%; background:linear-gradient(90deg, var(--accent), #6366f1); border-radius:11px; transition:width .4s ease; width:0%; }
        .bl-progress-text { position:absolute; inset:0; display:flex; align-items:center; justify-content:center; font-size:11px; font-weight:600; color:#fff; text-shadow:0 1px 2px rgba(0,0,0,.4); }
        .bl-progress-detail { font-size:12px; color:var(--text-muted); margin-top:6px; text-align:center; }
        .bl-progress-timer { font-size:11px; color:var(--text-muted); margin-top:4px; text-align:center; opacity:.7; }

        .bl-log { max-height:350px; overflow-y:auto; background:var(--bg-primary); border-radius:8px; padding:8px 10px; margin-top:10px; font-family:monospace; font-size:11px; color:var(--text-muted); }
        .bl-log:not(.visible) { display:none; }
        .bl-log-line { padding:2px 0; border-bottom:1px solid var(--border); }
        .bl-log-line:last-child { border:none; }

        .bl-result { text-align:center; padding:16px; margin-top:10px; display:none; }
        .bl-result-icon { font-size:40px; margin-bottom:8px; }

        .bl-artifacts { display:flex; flex-wrap:wrap; gap:10px; margin-top:10px; }
        .bl-artifact { background:var(--bg-primary); border:1px solid var(--border); border-radius:8px; padding:10px 14px; display:flex; align-items:center; gap:10px; font-size:12px; flex:1; min-width:200px; }
        .bl-artifact i { font-size:20px; color:var(--accent); }
        .bl-artifact-name { font-weight:600; color:var(--text-primary); }
        .bl-artifact-size { color:var(--text-muted); }
        .bl-artifact-del { margin-left:auto; color:var(--text-muted); cursor:pointer; padding:4px; }
        .bl-artifact-del:hover { color:#ef4444; }

        .bl-empty { text-align:center; padding:20px; color:var(--text-muted); font-size:13px; }

        .bl-warn { background:rgba(245,158,11,.1); border:1px solid rgba(245,158,11,.3); border-radius:8px; padding:10px 14px; font-size:12px; color:#f59e0b; display:flex; align-items:center; gap:8px; margin-bottom:12px; }

        .bl-toggle-log { font-size:11px; color:var(--text-muted); cursor:pointer; margin-top:6px; text-align:center; }
        .bl-toggle-log:hover { color:var(--accent); }
        .bl-cancel-wrap { text-align:center; margin-top:8px; }
        .bl-download-links { display:flex; gap:10px; justify-content:center; margin-top:10px; }
        .bl-download-links a { display:inline-flex; align-items:center; gap:6px; background:var(--accent); color:#fff; padding:6px 14px; border-radius:6px; text-decoration:none; font-size:12px; }
        .bl-download-links a:hover { filter:brightness(1.1); }

        .bl-spec-grid { display:grid; grid-template-columns:1fr 1fr; gap:12px; }
        .bl-spec-card { background:var(--bg-primary,#0f172a); border:1px solid var(--border); border-radius:8px; padding:14px; }
        .bl-spec-card-title { font-size:12px; font-weight:600; color:var(--text-secondary); text-transform:uppercase; letter-spacing:.5px; margin-bottom:10px; display:flex; align-items:center; gap:6px; }
        .bl-spec-card-title i { font-size:11px; color:var(--accent); }
        .bl-spec-field { margin-bottom:8px; }
        .bl-spec-field label { display:block; font-size:11px; color:var(--text-muted); margin-bottom:3px; }
        .bl-spec-field input, .bl-spec-field select { width:100%; box-sizing:border-box; }
        .bl-spec-pkgs { background:var(--bg-primary,#0f172a); border:1px solid var(--border); border-radius:6px; padding:8px 10px; font-family:monospace; font-size:11px; color:var(--text-secondary); max-height:140px; overflow-y:auto; line-height:1.6; white-space:pre-wrap; word-break:break-all; }
        .bl-spec-actions { display:flex; gap:8px; justify-content:flex-end; margin-top:12px; }
        .bl-spec-tag { display:inline-block; background:var(--bg-hover); border:1px solid var(--border); border-radius:4px; padding:2px 7px; font-size:11px; color:var(--text-secondary); margin:2px; }
        .bl-spec-tag .remove { cursor:pointer; margin-left:4px; color:var(--text-muted); }
        .bl-spec-tag .remove:hover { color:#ef4444; }
        .bl-spec-info { font-size:11px; color:var(--text-muted); margin-top:6px; }
    </style>

    <div class="bl-wrap">
        <div class="bl-sidebar">
            <div class="bl-nav active" data-tab="release"><i class="fas fa-box"></i> Release</div>
            <div class="bl-nav" data-tab="image"><i class="fas fa-hdd"></i> Obraz systemu</div>
            <div class="bl-nav" data-tab="publish"><i class="fas fa-cloud-upload-alt"></i> Publikuj apki</div>
            <div class="bl-nav" data-tab="artifacts"><i class="fas fa-archive"></i> Artefakty</div>
            <div class="bl-nav" data-tab="spec"><i class="fas fa-file-code"></i> Build Spec</div>
        </div>
        <div class="bl-body" id="bl-body"></div>
    </div>`;

    const $ = s => body.querySelector(s);
    const blBody = $('#bl-body');
    let _pollIv = null;    // polling interval for reconnect
    let _logSince = 0;     // log offset for polling

    /* ─── Tab switching ─── */
    body.querySelectorAll('.bl-nav').forEach(tab => {
        tab.onclick = () => {
            body.querySelectorAll('.bl-nav').forEach(t => t.classList.remove('active'));
            tab.classList.add('active');
            state.tab = tab.dataset.tab;
            renderTab();
        };
    });

    /* ─── Helpers ─── */
    function humanSize(b) {
        if (b >= 1e12) return (b / 1e12).toFixed(1) + ' TB';
        if (b >= 1e9)  return (b / 1e9).toFixed(1) + ' GB';
        if (b >= 1e6)  return (b / 1e6).toFixed(1) + ' MB';
        return (b / 1024).toFixed(0) + ' KB';
    }

    /* ─── Load info ─── */
    async function loadInfo() {
        try { state.info = await api('/builder/info'); } catch { state.info = null; }
        renderTab();
    }

    /* ─── Render tabs ─── */
    function renderTab() {
        if (state.tab === 'release') renderRelease();
        else if (state.tab === 'image') renderImage();
        else if (state.tab === 'publish') renderPublish();
        else if (state.tab === 'spec') renderSpec();
        else renderArtifacts();
    }

    /* ═══════════════════════════════════════════
       Release Tab
    ═══════════════════════════════════════════ */
    function renderRelease() {
        const ver = state.info?.version?.version || '?.?.?';
        blBody.innerHTML = `
            <div class="bl-section">
                <div class="bl-section-title"><i class="fas fa-tag"></i> Nowy Release</div>
                <div class="bl-row">
                    <label>Aktualna wersja:</label>
                    <span class="bl-ver"><i class="fas fa-code-branch"></i> ${ver}</span>
                </div>
                <div class="bl-row">
                    <label>${t('Podbij wersję:')}</label>
                    <select class="bl-select" id="bl-bump">
                        <option value="">Bez zmiany (${ver})</option>
                        <option value="patch" selected>Patch (${bumpVersion(ver,'patch')})</option>
                        <option value="minor">Minor (${bumpVersion(ver,'minor')})</option>
                        <option value="major">Major (${bumpVersion(ver,'major')})</option>
                    </select>
                </div>
                <div class="bl-row">
                    <label>${t('Tytuł zmian:')}</label>
                    <input class="bl-input" id="bl-cl-title" placeholder="${t('np. Kreator USB, poprawki błędów')}">
                </div>
                <div class="bl-row" style="align-items:flex-start">
                    <label>Lista zmian:</label>
                    <textarea class="bl-textarea" id="bl-cl-changes" placeholder="${t('Jedna zmiana per linia&#10;np. Nowa aplikacja Kreator USB&#10;Poprawka limitów pobierania')}"></textarea>
                </div>
                <div style="text-align:right;margin-top:6px">
                    <button class="bl-btn bl-btn-green" id="bl-release-btn"><i class="fas fa-rocket"></i> Zbuduj release</button>
                </div>
            </div>
            <div class="bl-progress-wrap" id="bl-progress">
                <div class="bl-progress-outer">
                    <div class="bl-progress-inner" id="bl-bar"></div>
                    <div class="bl-progress-text" id="bl-bar-text">0%</div>
                </div>
                <div class="bl-progress-detail" id="bl-detail"></div>
                <div class="bl-progress-timer" id="bl-timer"></div>
                <div class="bl-toggle-log" id="bl-toggle-log">${t('Pokaż logi ▼')}</div>
            </div>
            <div class="bl-log" id="bl-log"></div>
            <div class="bl-result" id="bl-result"></div>`;

        blBody.querySelector('#bl-release-btn').onclick = startRelease;
        blBody.querySelector('#bl-toggle-log').onclick = () => {
            const log = blBody.querySelector('#bl-log');
            log.classList.toggle('visible');
        };
    }

    function bumpVersion(v, type) {
        const [maj, mi, pat] = (v || '0.0.0').split('.').map(Number);
        if (type === 'major') return `${maj+1}.0.0`;
        if (type === 'minor') return `${maj}.${mi+1}.0`;
        return `${maj}.${mi}.${pat+1}`;
    }

    async function startRelease() {
        if (state.building) return;
        state.building = true;

        const bump = blBody.querySelector('#bl-bump').value;
        const title = blBody.querySelector('#bl-cl-title').value.trim();
        const changesRaw = blBody.querySelector('#bl-cl-changes').value.trim();
        const changes = changesRaw ? changesRaw.split('\n').map(l => l.trim()).filter(Boolean) : [];

        setDisabled(true);

        const bar = blBody.querySelector('#bl-bar');
        const barText = blBody.querySelector('#bl-bar-text');
        const detail = blBody.querySelector('#bl-detail');
        const logEl = blBody.querySelector('#bl-log');
        const resultEl = blBody.querySelector('#bl-result');
        const progressW = blBody.querySelector('#bl-progress');

        progressW.classList.add('active');
        logEl.innerHTML = '';
        logEl.classList.remove('visible');
        resultEl.style.display = 'none';
        bar.style.width = '0%';
        barText.textContent = '0%';
        detail.textContent = 'Rozpoczynanie...';
        const timerEl = blBody.querySelector('#bl-timer');
        const buildStart = Date.now();
        const timerIv = setInterval(() => {
            const s = Math.floor((Date.now() - buildStart) / 1000);
            const m = Math.floor(s / 60);
            timerEl.textContent = `⏱ ${m}:${String(s % 60).padStart(2, '0')}`;
        }, 1000);
        showCancelBtn();

        try {
            const headers = {};
            if (NAS.token) headers['Authorization'] = `Bearer ${NAS.token}`;
            if (NAS.csrfToken) headers['X-CSRFToken'] = NAS.csrfToken;
            headers['Content-Type'] = 'application/json';

            const resp = await fetch('/api/builder/release', {
                method: 'POST', headers,
                body: JSON.stringify({ bump, changelog_title: title, changelog_changes: changes }),
            });

            if (!resp.ok) {
                const err = await resp.json().catch(() => ({}));
                showResult(resultEl, false, err.error || t('Błąd'));
                return;
            }

            await readSSE(resp, bar, barText, detail, logEl, resultEl);
        } catch (e) {
            // SSE broken — fall back to polling
            _logSince = 0;
            startPolling();
            return;
        } finally {
            clearInterval(timerIv);
            if (!_pollIv) {
                state.building = false;
                setDisabled(false);
            }
        }
    }

    /* ═══════════════════════════════════════════
       Publish Apps Tab
    ═══════════════════════════════════════════ */
    function renderPublish() {
        blBody.innerHTML = `
            <div class="bl-section">
                <div class="bl-section-title"><i class="fas fa-cloud-upload-alt"></i> Publikuj aplikacje do GitHub</div>
                <div style="color:var(--text-secondary);font-size:12px;margin-bottom:12px;line-height:1.5">
                    ${t('Porównaj lokalne pliki opcjonalnych aplikacji z repozytorium GitHub.')}
                    ${t('Zmienione aplikacje zostaną opublikowane z automatycznym podbiciem wersji patch.')}
                </div>
                <div class="bl-row">
                    <label>GitHub Token:</label>
                    <input class="bl-input" id="bl-pub-token" type="password" placeholder="ghp_..." style="max-width:320px">
                    <button class="bl-btn bl-btn-sm bl-btn-outline" id="bl-pub-save-token"><i class="fas fa-save"></i> Zapisz</button>
                </div>
                <div class="bl-row">
                    <label>Repozytorium:</label>
                    <input class="bl-input" id="bl-pub-repo" value="SyncHot/ethos-os-ethos-apps" style="max-width:320px" readonly>
                </div>
            </div>
            <div class="bl-section" id="bl-pub-diff-section">
                <div class="bl-section-title">
                    <i class="fas fa-code-compare"></i> Zmiany do opublikowania
                    <button class="bl-btn bl-btn-sm bl-btn-outline" id="bl-pub-refresh" style="margin-left:auto"><i class="fas fa-sync-alt"></i> Odśwież</button>
                </div>
                <div id="bl-pub-diff-body" style="font-size:12px;color:var(--text-muted)">Ładowanie...</div>
            </div>
            <div style="text-align:right;margin-top:6px">
                <button class="bl-btn bl-btn-outline bl-btn-sm" id="bl-pub-select-all" style="margin-right:8px"><i class="fas fa-check-double"></i> Zaznacz zmienione</button>
                <button class="bl-btn bl-btn-green" id="bl-pub-btn" disabled><i class="fas fa-cloud-upload-alt"></i> Opublikuj zaznaczone</button>
            </div>
            <div class="bl-progress-wrap" id="bl-pub-progress">
                <div class="bl-progress-outer">
                    <div class="bl-progress-inner" id="bl-pub-bar"></div>
                    <div class="bl-progress-text" id="bl-pub-bar-text">0%</div>
                </div>
                <div class="bl-progress-detail" id="bl-pub-detail"></div>
                <div class="bl-progress-timer" id="bl-pub-timer"></div>
                <div class="bl-toggle-log" id="bl-pub-toggle-log">${t('Pokaż logi ▼')}</div>
            </div>
            <div class="bl-log" id="bl-pub-log"></div>
            <div class="bl-result" id="bl-pub-result"></div>`;

        const tokenEl = blBody.querySelector('#bl-pub-token');
        const repoEl = blBody.querySelector('#bl-pub-repo');
        let _pubApps = [];

        // Load config
        (async () => {
            try {
                const cfg = await api('/builder/publish-config');
                if (cfg.has_token) tokenEl.value = cfg.token;
                if (cfg.repo) repoEl.value = cfg.repo;
            } catch {}
            loadPublishDiff();
        })();

        // Save token
        blBody.querySelector('#bl-pub-save-token').onclick = async () => {
            const token = tokenEl.value.trim();
            if (!token || token.includes('***')) {
                toast(t('Wpisz nowy token'), 'warning');
                return;
            }
            try {
                await api('/builder/publish-config', {
                    method: 'PUT',
                    body: { token, repo: repoEl.value.trim() },
                });
                toast(t('Token zapisany'), 'success');
                loadPublishDiff();
            } catch (e) {
                toast(e.message || t('Błąd'), 'error');
            }
        };

        // Refresh diff
        blBody.querySelector('#bl-pub-refresh').onclick = () => loadPublishDiff();

        // Select all changed
        blBody.querySelector('#bl-pub-select-all').onclick = () => {
            blBody.querySelectorAll('.bl-pub-check').forEach(cb => {
                if (cb.dataset.changed === '1') cb.checked = true;
            });
            updatePublishBtn();
        };

        // Publish button
        blBody.querySelector('#bl-pub-btn').onclick = startPublish;

        // Toggle log
        blBody.querySelector('#bl-pub-toggle-log').onclick = () => {
            blBody.querySelector('#bl-pub-log').classList.toggle('visible');
        };

        function updatePublishBtn() {
            const checked = blBody.querySelectorAll('.bl-pub-check:checked').length;
            blBody.querySelector('#bl-pub-btn').disabled = checked === 0;
        }

        async function loadPublishDiff() {
            const diffBody = blBody.querySelector('#bl-pub-diff-body');
            diffBody.innerHTML = '<i class="fas fa-spinner fa-spin"></i> Porównywanie z GitHub...';

            try {
                const data = await api('/builder/publish-diff');
                if (!data.ok) {
                    diffBody.innerHTML = `<span style="color:#ef4444">${data.error || 'Błąd'}</span>`;
                    return;
                }
                if (!data.has_token) {
                    diffBody.innerHTML = `<span style="color:#f59e0b"><i class="fas fa-key"></i> Skonfiguruj GitHub Token powyżej, aby porównać pliki.</span>`;
                    return;
                }
                _pubApps = data.apps || [];
                if (!_pubApps.length) {
                    diffBody.innerHTML = '<span style="color:var(--text-muted)">Brak opcjonalnych aplikacji.</span>';
                    return;
                }
                const changedCount = _pubApps.filter(a => a.changed).length;
                let html = `<div style="margin-bottom:8px;color:var(--text-secondary)">
                    ${changedCount > 0
                        ? `<span style="color:#f59e0b"><i class="fas fa-exclamation-circle"></i> ${changedCount} zmienion${changedCount === 1 ? 'a' : changedCount < 5 ? 'e' : 'ych'}</span> / ${_pubApps.length} aplikacji`
                        : `<span style="color:#10b981"><i class="fas fa-check-circle"></i> Wszystkie aplikacje aktualne</span>`}
                </div>`;

                html += `<table style="width:100%;border-collapse:collapse;font-size:12px">
                    <thead><tr style="border-bottom:1px solid var(--border);color:var(--text-muted);text-align:left">
                        <th style="padding:6px 4px;width:30px"></th>
                        <th style="padding:6px 4px">Aplikacja</th>
                        <th style="padding:6px 4px;width:80px">Lokalna</th>
                        <th style="padding:6px 4px;width:80px">GitHub</th>
                        <th style="padding:6px 4px;width:120px">Status</th>
                    </tr></thead><tbody>`;

                for (const app of _pubApps) {
                    const statusBadges = app.changes.map(c => {
                        const color = c.status === 'new' ? '#10b981' : '#f59e0b';
                        const icon = c.status === 'new' ? 'fa-plus' : 'fa-pen';
                        return `<span style="display:inline-flex;align-items:center;gap:3px;background:${color}22;color:${color};padding:1px 6px;border-radius:4px;font-size:11px"><i class="fas ${icon}" style="font-size:9px"></i>${c.file}</span>`;
                    }).join(' ');

                    const verStyle = app.changed ? 'color:#f59e0b;font-weight:600' : 'color:var(--text-muted)';
                    html += `<tr style="border-bottom:1px solid var(--border)">
                        <td style="padding:6px 4px;text-align:center">
                            <input type="checkbox" class="bl-pub-check" data-id="${app.id}" data-changed="${app.changed ? 1 : 0}" ${app.changed ? '' : 'disabled'}>
                        </td>
                        <td style="padding:6px 4px">
                            <span style="display:inline-flex;align-items:center;gap:6px">
                                <i class="fas ${app.icon}" style="color:${app.color};width:14px;text-align:center;font-size:11px"></i>
                                <strong>${app.name}</strong>
                            </span>
                        </td>
                        <td style="padding:6px 4px;${verStyle}">${app.local_version}</td>
                        <td style="padding:6px 4px;color:var(--text-muted)">${app.remote_version}</td>
                        <td style="padding:6px 4px">${app.changed ? statusBadges : '<span style="color:#10b981;font-size:11px"><i class="fas fa-check"></i> OK</span>'}</td>
                    </tr>`;
                }
                html += '</tbody></table>';
                diffBody.innerHTML = html;

                // Wire up checkboxes
                blBody.querySelectorAll('.bl-pub-check').forEach(cb => {
                    cb.onchange = updatePublishBtn;
                });
            } catch (e) {
                diffBody.innerHTML = `<span style="color:#ef4444">Błąd: ${e.message || e}</span>`;
            }
        }

        async function startPublish() {
            const selected = [];
            blBody.querySelectorAll('.bl-pub-check:checked').forEach(cb => selected.push(cb.dataset.id));
            if (!selected.length) return;
            if (!await confirmDialog(t('Opublikować ' + selected.length + ' aplikacji do GitHub?'))) return;

            state.building = true;
            setDisabled(true);

            const bar = blBody.querySelector('#bl-pub-bar');
            const barText = blBody.querySelector('#bl-pub-bar-text');
            const detail = blBody.querySelector('#bl-pub-detail');
            const logEl = blBody.querySelector('#bl-pub-log');
            const resultEl = blBody.querySelector('#bl-pub-result');
            const timerEl = blBody.querySelector('#bl-pub-timer');
            const progress = blBody.querySelector('#bl-pub-progress');

            bar.style.width = '0%';
            barText.textContent = '0%';
            detail.textContent = '';
            logEl.innerHTML = '';
            logEl.classList.remove('visible');
            resultEl.style.display = 'none';
            progress.classList.add('active');

            const buildStart = Date.now();
            const timerIv = setInterval(() => {
                const s = Math.floor((Date.now() - buildStart) / 1000);
                const m = Math.floor(s / 60);
                timerEl.textContent = `⏱ ${m}:${String(s % 60).padStart(2, '0')}`;
            }, 1000);

            try {
                const resp = await fetch('/api/builder/publish-apps', {
                    method: 'POST',
                    headers: {
                        'Content-Type': 'application/json',
                        'Authorization': 'Bearer ' + NAS.token,
                        'X-CSRFToken': NAS.csrfToken || '',
                    },
                    body: JSON.stringify({ app_ids: selected }),
                });
                await readSSE(resp, bar, barText, detail, logEl, resultEl);
            } catch (e) {
                showResult(resultEl, false, 'Błąd połączenia: ' + e.message);
            } finally {
                clearInterval(timerIv);
                state.building = false;
                setDisabled(false);
            }
        }
    }

    /* ═══════════════════════════════════════════
       Image Tab
    ═══════════════════════════════════════════ */
    function renderImage() {
        blBody.innerHTML = `
            <div class="bl-warn"><i class="fas fa-exclamation-triangle"></i> ${t('Budowanie obrazu wymaga ~15-30 minut i dostępu do internetu. Proces pobiera pakiety Debian.')}</div>
            <div class="bl-section">
                <div class="bl-section-title"><i class="fas fa-hdd"></i> Nowy obraz systemu (x86_64)</div>

                <div style="color:var(--text-secondary);font-size:13px;margin:8px 0 12px;line-height:1.5;">
                    <i class="fas fa-info-circle"></i> ${t('Użytkownik, hasło i hostname zostaną ustawione przez kreator przy pierwszym uruchomieniu obrazu.')}
                </div>
                <div style="text-align:right;margin-top:6px">
                    <button class="bl-btn bl-btn-danger" id="bl-image-btn"><i class="fas fa-compact-disc"></i> Zbuduj obraz</button>
                </div>
            </div>
            <div class="bl-progress-wrap" id="bl-progress">
                <div class="bl-progress-outer">
                    <div class="bl-progress-inner" id="bl-bar"></div>
                    <div class="bl-progress-text" id="bl-bar-text">0%</div>
                </div>
                <div class="bl-progress-detail" id="bl-detail"></div>
                <div class="bl-progress-timer" id="bl-timer"></div>
                <div class="bl-toggle-log" id="bl-toggle-log">${t('Pokaż logi ▼')}</div>
            </div>
            <div class="bl-log" id="bl-log"></div>
            <div class="bl-result" id="bl-result"></div>`;

        blBody.querySelector('#bl-image-btn').onclick = startImage;
        blBody.querySelector('#bl-toggle-log').onclick = () => {
            const log = blBody.querySelector('#bl-log');
            log.classList.toggle('visible');
        };

        // Show last result if available
        if (state._lastResult && state._lastResult.build_type === 'image') {
            const r = state._lastResult;
            const resultEl = blBody.querySelector('#bl-result');
            const progressW = blBody.querySelector('#bl-progress');
            const bar = blBody.querySelector('#bl-bar');
            const barText = blBody.querySelector('#bl-bar-text');
            const detail = blBody.querySelector('#bl-detail');
            progressW.classList.add('active');
            bar.style.width = r.percent + '%';
            barText.textContent = r.percent + '%';
            detail.textContent = r.message || '';
            const res = r.result || {};
            showResult(resultEl, res.success, res.message || r.message, res);
        }
    }

    async function startImage() {
        if (state.building) return;

        if (!await confirmDialog(t('Budowanie obrazu x86_64 zajmie ~15-30 minut.') + '\n' + t('Kontynuować?'))) return;

        state.building = true;
        setDisabled(true);

        const bar = blBody.querySelector('#bl-bar');
        const barText = blBody.querySelector('#bl-bar-text');
        const detail = blBody.querySelector('#bl-detail');
        const logEl = blBody.querySelector('#bl-log');
        const resultEl = blBody.querySelector('#bl-result');
        const progressW = blBody.querySelector('#bl-progress');

        progressW.classList.add('active');
        logEl.innerHTML = '';
        logEl.classList.remove('visible');
        resultEl.style.display = 'none';
        bar.style.width = '0%';
        barText.textContent = '0%';
        detail.textContent = 'Przygotowywanie...';
        const timerEl = blBody.querySelector('#bl-timer');
        const buildStart = Date.now();
        const timerIv = setInterval(() => {
            const s = Math.floor((Date.now() - buildStart) / 1000);
            const m = Math.floor(s / 60);
            timerEl.textContent = `⏱ ${m}:${String(s % 60).padStart(2, '0')}`;
        }, 1000);
        showCancelBtn();

        try {
            const res = await api('/builder/image', {
                method: 'POST',
                body: { type: 'x86' },
            });

            if (res.error) {
                showResult(resultEl, false, res.error);
                clearInterval(timerIv);
                state.building = false;
                setDisabled(false);
                return;
            }

            // Build started in background — poll for updates
            _logSince = 0;
            // Immediate first status check, then poll every 3s
            try {
                const st = await api(`/builder/status?since=0`);
                if (st.percent > 0) {
                    bar.style.width = st.percent + '%';
                    barText.textContent = st.percent + '%';
                    detail.textContent = st.message || '';
                }
                if (st.logs && st.logs.length) {
                    logEl.classList.add('visible');
                    for (const l of st.logs) addLog(logEl, l);
                    _logSince = st.log_total || st.logs.length;
                }
            } catch {}
            startPolling();
        } catch (e) {
            showResult(resultEl, false, e.message || t('Błąd'));
            clearInterval(timerIv);
            state.building = false;
            setDisabled(false);
        }
    }

    /* ═══════════════════════════════════════════
       Artifacts Tab  (multi-select delete)
    ═══════════════════════════════════════════ */
    function renderArtifacts() {
        const rels = state.info?.releases || [];
        const imgs = state.info?.images || [];
        const latest = state.info?.latest;

        let html = '';

        // Latest release info
        if (latest) {
            html += `
            <div class="bl-section">
                <div class="bl-section-title"><i class="fas fa-tag"></i> Ostatni release</div>
                <div style="display:flex;gap:12px;align-items:center">
                    <span class="bl-ver"><i class="fas fa-code-branch"></i> ${latest.version}</span>
                    <span style="color:var(--text-muted);font-size:12px">${latest.build_date || ''} • ${humanSize(latest.size || 0)} • SHA256: ${(latest.sha256 || '').substring(0, 16)}…</span>
                </div>
            </div>`;
        }

        // Releases
        html += `<div class="bl-section">
            <div class="bl-section-title" style="justify-content:space-between">
                <span><i class="fas fa-box"></i> Pakiety release</span>
                <span class="bl-sel-actions" data-group="rel" style="display:none">
                    <button class="bl-btn bl-btn-sm bl-btn-danger bl-bulk-del" data-group="rel"><i class="fas fa-trash"></i> ${t('Usuń zaznaczone')}</button>
                </span>
            </div>`;
        if (rels.length === 0) {
            html += '<div class="bl-empty"><i class="fas fa-inbox" style="font-size:24px;margin-bottom:8px;display:block"></i>Brak zbudowanych release</div>';
        } else {
            if (rels.length > 1) html += `<div style="margin-bottom:8px"><label style="font-size:11px;color:var(--text-muted);cursor:pointer;display:inline-flex;align-items:center;gap:4px"><input type="checkbox" class="bl-sel-all" data-group="rel"> Zaznacz wszystkie</label></div>`;
            html += '<div class="bl-artifacts">';
            for (const r of rels) {
                html += `<div class="bl-artifact">
                    <input type="checkbox" class="bl-sel" data-group="rel" data-path="${r.path}" style="margin-right:4px">
                    <i class="fas fa-file-archive"></i>
                    <div>
                        <div class="bl-artifact-name">${r.name}</div>
                        <div class="bl-artifact-size">${humanSize(r.size)}</div>
                    </div>
                    <a href="/api/builder/download?path=${encodeURIComponent(r.path)}&token=${encodeURIComponent(NAS.token)}" title="Pobierz" style="color:var(--accent);margin-left:auto;padding:4px"><i class="fas fa-download"></i></a>
                    <span class="bl-artifact-del" data-path="${r.path}" title="${t('Usuń')}"><i class="fas fa-trash"></i></span>
                </div>`;
            }
            html += '</div>';
        }
        html += '</div>';

        // Images
        html += `<div class="bl-section">
            <div class="bl-section-title" style="justify-content:space-between">
                <span><i class="fas fa-hdd"></i> Obrazy systemu</span>
                <span class="bl-sel-actions" data-group="img" style="display:none">
                    <button class="bl-btn bl-btn-sm bl-btn-danger bl-bulk-del" data-group="img"><i class="fas fa-trash"></i> ${t('Usuń zaznaczone')}</button>
                </span>
            </div>`;
        if (imgs.length === 0) {
            html += `<div class="bl-empty"><i class="fas fa-hdd" style="font-size:24px;margin-bottom:8px;display:block"></i>${t('Brak zbudowanych obrazów')}</div>`;
        } else {
            if (imgs.length > 1) html += `<div style="margin-bottom:8px"><label style="font-size:11px;color:var(--text-muted);cursor:pointer;display:inline-flex;align-items:center;gap:4px"><input type="checkbox" class="bl-sel-all" data-group="img"> Zaznacz wszystkie</label></div>`;
            html += '<div class="bl-artifacts">';
            for (const i of imgs) {
                const icon = i.name.endsWith('.iso') ? 'fa-compact-disc' : 'fa-hdd';
                html += `<div class="bl-artifact">
                    <input type="checkbox" class="bl-sel" data-group="img" data-path="${i.path}" style="margin-right:4px">
                    <i class="fas ${icon}"></i>
                    <div>
                        <div class="bl-artifact-name">${i.name}</div>
                        <div class="bl-artifact-size">${humanSize(i.size)}</div>
                    </div>
                    <a href="/api/builder/download?path=${encodeURIComponent(i.path)}&token=${encodeURIComponent(NAS.token)}" title="Pobierz" style="color:var(--accent);margin-left:auto;padding:4px"><i class="fas fa-download"></i></a>
                    <span class="bl-artifact-del" data-path="${i.path}" title="${t('Usuń')}"><i class="fas fa-trash"></i></span>
                </div>`;
            }
            html += '</div>';
        }
        html += '</div>';

        // Build History
        html += `<div class="bl-section">
            <div class="bl-section-title" style="justify-content:space-between">
                <span><i class="fas fa-history"></i> ${t('Historia budowań')}</span>
                <button class="bl-btn bl-btn-sm bl-btn-outline" id="bl-history-clear" style="font-size:11px;color:#ef4444;border-color:#ef4444"><i class="fas fa-trash"></i></button>
            </div>
            <div id="bl-history-list" style="font-size:12px;color:var(--text-muted)">${t('Ładowanie…')}</div>
        </div>`;

        // Build cache
        html += `<div class="bl-section">
            <div class="bl-section-title"><i class="fas fa-database"></i> Cache budowania</div>
            <div id="bl-cache-info" style="font-size:12px;color:var(--text-muted)">${t('Ładowanie…')}</div>
            <div style="margin-top:8px"><button class="bl-btn bl-btn-sm bl-btn-outline" id="bl-cache-clear" style="color:#ef4444;border-color:#ef4444"><i class="fas fa-trash"></i> ${t('Wyczyść cache')}</button></div>
        </div>`;

        html += `<div style="text-align:center;margin-top:8px"><button class="bl-btn bl-btn-outline bl-btn-sm" id="bl-refresh-arts"><i class="fas fa-sync-alt"></i> ${t('Odśwież')}</button></div>`;

        blBody.innerHTML = html;

        /* ─── Selection logic ─── */
        function updateSelActions(group) {
            const checked = blBody.querySelectorAll(`.bl-sel[data-group="${group}"]:checked`);
            const actions = blBody.querySelector(`.bl-sel-actions[data-group="${group}"]`);
            if (actions) actions.style.display = checked.length > 0 ? '' : 'none';
        }

        // Individual checkboxes
        blBody.querySelectorAll('.bl-sel').forEach(cb => {
            cb.onchange = () => updateSelActions(cb.dataset.group);
        });

        // Select-all checkboxes
        blBody.querySelectorAll('.bl-sel-all').forEach(sa => {
            sa.onchange = () => {
                const g = sa.dataset.group;
                blBody.querySelectorAll(`.bl-sel[data-group="${g}"]`).forEach(cb => cb.checked = sa.checked);
                updateSelActions(g);
            };
        });

        // Bulk delete buttons
        blBody.querySelectorAll('.bl-bulk-del').forEach(btn => {
            btn.onclick = async () => {
                const g = btn.dataset.group;
                const paths = [...blBody.querySelectorAll(`.bl-sel[data-group="${g}"]:checked`)].map(cb => cb.dataset.path);
                if (!paths.length) return;
                const label = g === 'rel' ? t('pakietów') : t('obrazów');
                if (!await confirmDialog(t('Usunąć') + ` ${paths.length} ${label}?`)) return;
                try {
                    const res = await api('/builder/delete', { method: 'POST', body: { paths } });
                    if (res.deleted?.length) toast(`${t('Usunięto')} ${res.deleted.length} ${t('plików')}`, 'success');
                    if (res.errors?.length) toast(res.errors.join(', '), 'error');
                    loadInfo();
                } catch (e) { toast(e.message, 'error'); }
            };
        });

        // Single delete buttons
        blBody.querySelectorAll('.bl-artifact-del').forEach(btn => {
            btn.onclick = async () => {
                const path = btn.dataset.path;
                if (!await confirmDialog(t('Usunąć') + ` ${path.split('/').pop()}?`)) return;
                try {
                    const res = await api('/builder/delete', { method: 'POST', body: { path } });
                    if (res.ok) { toast(t('Usunięto'), 'success'); loadInfo(); }
                    else toast(res.error || t('Błąd'), 'error');
                } catch (e) { toast(e.message, 'error'); }
            };
        });

        const refreshBtn = blBody.querySelector('#bl-refresh-arts');
        if (refreshBtn) refreshBtn.onclick = loadInfo;

        // Cache info
        (async () => {
            try {
                const c = await api('/builder/cache');
                const el = blBody.querySelector('#bl-cache-info');
                if (el) {
                    const parts = Object.entries(c.cache || {}).map(([k, v]) => `${k}: ${v}`).join(', ');
                    el.textContent = parts || 'Pusty';
                }
            } catch {}
        })();
        const cacheBtn = blBody.querySelector('#bl-cache-clear');
        if (cacheBtn) cacheBtn.onclick = async () => {
            if (!await confirmDialog(t('Wyczyścić cache? Następny build pobierze pakiety od nowa.'))) return;
            await api('/builder/cache', { method: 'DELETE' });
            toast('Cache wyczyszczony', 'success');
            loadInfo();
        };

        // Build History
        (async () => {
            try {
                const h = await api('/builder/history');
                const el = blBody.querySelector('#bl-history-list');
                if (!el) return;
                const items = h.items || [];
                if (!items.length) {
                    el.innerHTML = `<div style="text-align:center;color:var(--text-muted);padding:12px">${t('Brak historii')}</div>`;
                    return;
                }
                let rows = '';
                for (const b of items) {
                    const ok = b.result?.success;
                    const icon = ok ? '<i class="fas fa-check-circle" style="color:#10b981"></i>' : '<i class="fas fa-times-circle" style="color:#ef4444"></i>';
                    const type = b.build_type === 'release' ? '<i class="fas fa-tag"></i> Release' : '<i class="fas fa-hdd"></i> Image';
                    const dt = b.end_time ? new Date(b.end_time * 1000).toLocaleString('pl-PL') : '—';
                    const dur = b.duration ? `${Math.floor(b.duration / 60)}m ${b.duration % 60}s` : '—';
                    const msg = b.message || '';
                    rows += `<div style="display:flex;align-items:center;gap:8px;padding:6px 0;border-bottom:1px solid var(--border-color)">
                        ${icon}
                        <span style="min-width:80px">${type}</span>
                        <span style="color:var(--text-secondary);min-width:130px;font-size:11px">${dt}</span>
                        <span style="color:var(--text-muted);min-width:60px;font-size:11px">${dur}</span>
                        <span style="flex:1;overflow:hidden;text-overflow:ellipsis;white-space:nowrap;font-size:11px" title="${msg.replace(/"/g, '&quot;')}">${msg}</span>
                    </div>`;
                }
                el.innerHTML = rows;
            } catch {}
        })();
        const histClearBtn = blBody.querySelector('#bl-history-clear');
        if (histClearBtn) histClearBtn.onclick = async () => {
            if (!await confirmDialog(t('Wyczyścić historię budowań?'))) return;
            await api('/builder/history/clear', { method: 'POST' });
            toast(t('Historia wyczyszczona'), 'success');
            renderArtifacts();
        };
    }

    /* ─── SSE Reader ─── */
    async function readSSE(resp, bar, barText, detail, logEl, resultEl) {
        const reader = resp.body.getReader();
        const decoder = new TextDecoder();
        let buf = '';

        while (true) {
            const { done, value } = await reader.read();
            if (done) break;
            buf += decoder.decode(value, { stream: true });
            const lines = buf.split('\n');
            buf = lines.pop();
            for (const line of lines) {
                if (!line.startsWith('data: ')) continue;
                try {
                    const ev = JSON.parse(line.slice(6));
                    switch (ev.type) {
                        case 'step':
                            if (ev.percent != null) {
                                bar.style.width = ev.percent + '%';
                                barText.textContent = ev.percent + '%';
                            }
                            detail.textContent = ev.message || '';
                            addLog(logEl, ev.message);
                            break;
                        case 'progress':
                            if (ev.percent != null) {
                                bar.style.width = ev.percent + '%';
                                barText.textContent = ev.percent + '%';
                            }
                            if (ev.message) detail.textContent = ev.message;
                            break;
                        case 'log':
                            addLog(logEl, ev.message);
                            break;
                        case 'done':
                            if (ev.percent != null) {
                                bar.style.width = ev.percent + '%';
                                barText.textContent = ev.percent + '%';
                            }
                            showResult(resultEl, ev.success, ev.message);
                            loadInfo();
                            break;
                    }
                } catch {}
            }
        }
    }

    function addLog(logEl, msg) {
        if (!msg) return;
        logEl.classList.add('visible');
        logEl.innerHTML += `<div class="bl-log-line">${msg}</div>`;
        logEl.scrollTop = logEl.scrollHeight;
    }

    function showResult(el, success, msg, res) {
        el.style.display = '';
        const dismissBtn = `<div style="margin-top:12px"><button class="bl-btn bl-btn-sm" id="bl-dismiss-btn"><i class="fas fa-times"></i> Zamknij</button></div>`;
        if (success) {
            let links = '';
            if (res && res.img) {
                links += `<div class="bl-download-links">`;
                if (res.img) links += `<a href="/api/builder/download?path=${encodeURIComponent(res.img)}&token=${encodeURIComponent(NAS.token)}"><i class="fas fa-download"></i> Pobierz .img</a>`;
                if (res.iso) links += `<a href="/api/builder/download?path=${encodeURIComponent(res.iso)}&token=${encodeURIComponent(NAS.token)}"><i class="fas fa-download"></i> Pobierz .iso</a>`;
                links += `</div>`;
            }
            el.innerHTML = `<div class="bl-result-icon" style="color:#10b981"><i class="fas fa-check-circle"></i></div>
                <div style="font-weight:600;color:#10b981">${msg}</div>${links}${dismissBtn}`;
            toast(t('Budowanie zakończone!'), 'success');
        } else {
            el.innerHTML = `<div class="bl-result-icon" style="color:#ef4444"><i class="fas fa-times-circle"></i></div>
                <div style="font-weight:600;color:#ef4444">${t('Błąd')}</div>
                <div style="font-size:12px;color:var(--text-muted);margin-top:4px">${msg}</div>${dismissBtn}`;
            toast(t('Budowanie nie powiodło się'), 'error');
        }
        const btn = el.querySelector('#bl-dismiss-btn');
        if (btn) btn.onclick = async () => {
            try { await api('/builder/dismiss', { method: 'POST' }); } catch {}
            state._lastResult = null;
            el.style.display = 'none';
            const progressW = blBody.querySelector('#bl-progress');
            if (progressW) progressW.classList.remove('active');
            const cancelW = blBody.querySelector('.bl-cancel-wrap');
            if (cancelW) cancelW.remove();
        };
    }

    function setDisabled(d) {
        body.querySelectorAll('.bl-btn, .bl-input, .bl-select, .bl-textarea, .bl-nav').forEach(el => {
            if (d) el.style.pointerEvents = 'none';
            else el.style.pointerEvents = '';
        });
        body.querySelectorAll('.bl-btn').forEach(btn => btn.disabled = d);
    }

    /* ─── Check for running build & reconnect ─── */
    async function checkBuildStatus() {
        try {
            const st = await api('/builder/status');
            if (st.status === 'building') {
                state.building = true;
                const tabName = st.build_type === 'release' ? 'release' : 'image';
                state.tab = tabName;
                body.querySelectorAll('.bl-nav').forEach(t => {
                    t.classList.toggle('active', t.dataset.tab === tabName);
                });
                renderTab();
                showReconnectedProgress(st);
                startPolling();
            } else if (st.status === 'done' || st.status === 'error') {
                // Show last result on matching tab
                state._lastResult = st;
            }
        } catch {}
    }

    function showReconnectedProgress(st) {
        const bar = blBody.querySelector('#bl-bar');
        const barText = blBody.querySelector('#bl-bar-text');
        const detail = blBody.querySelector('#bl-detail');
        const logEl = blBody.querySelector('#bl-log');
        const progressW = blBody.querySelector('#bl-progress');
        const timerEl = blBody.querySelector('#bl-timer');

        if (!bar || !progressW) return;

        progressW.classList.add('active');
        bar.style.width = st.percent + '%';
        barText.textContent = st.percent + '%';
        detail.textContent = st.message || '';

        if (st.elapsed) {
            const m = Math.floor(st.elapsed / 60);
            const s = st.elapsed % 60;
            timerEl.textContent = `⏱ ${m}:${String(s).padStart(2, '0')}`;
        }

        logEl.innerHTML = '';
        if (st.logs && st.logs.length) {
            logEl.classList.add('visible');
            for (const l of st.logs) addLog(logEl, l);
        }
        _logSince = st.log_total || 0;
        setDisabled(true);
        showCancelBtn();
    }

    function startPolling() {
        if (_pollIv) return;
        const startServer = Date.now();
        _pollIv = setInterval(async () => {
            try {
                const st = await api(`/builder/status?since=${_logSince}`);
                const bar = blBody.querySelector('#bl-bar');
                const barText = blBody.querySelector('#bl-bar-text');
                const detail = blBody.querySelector('#bl-detail');
                const logEl = blBody.querySelector('#bl-log');
                const timerEl = blBody.querySelector('#bl-timer');
                const resultEl = blBody.querySelector('#bl-result');

                if (!bar) { stopPolling(); return; }

                bar.style.width = st.percent + '%';
                barText.textContent = st.percent + '%';
                detail.textContent = st.message || '';

                if (st.elapsed) {
                    const m = Math.floor(st.elapsed / 60);
                    const s = st.elapsed % 60;
                    timerEl.textContent = `⏱ ${m}:${String(s).padStart(2, '0')}`;
                }

                if (st.logs && st.logs.length) {
                    for (const l of st.logs) addLog(logEl, l);
                    _logSince = st.log_total || (_logSince + st.logs.length);
                }

                if (st.status !== 'building') {
                    stopPolling();
                    state.building = false;
                    setDisabled(false);
                    const res = st.result || {};
                    showResult(resultEl, res.success, res.message || st.message, res);
                    loadInfo();
                }
            } catch {}
        }, 3000);
    }

    function stopPolling() {
        if (_pollIv) { clearInterval(_pollIv); _pollIv = null; }
    }

    function showCancelBtn() {
        let wrap = blBody.querySelector('.bl-cancel-wrap');
        if (wrap) return;
        const progress = blBody.querySelector('#bl-progress');
        if (!progress) return;
        wrap = document.createElement('div');
        wrap.className = 'bl-cancel-wrap';
        wrap.innerHTML = '<button class="bl-btn bl-btn-cancel bl-btn-sm" id="bl-cancel-btn"><i class="fas fa-stop"></i> Anuluj build</button>';
        progress.appendChild(wrap);
        wrap.querySelector('#bl-cancel-btn').onclick = cancelBuild;
    }

    async function cancelBuild() {
        if (!await confirmDialog(t('Na pewno anulować bieżący build?'))) return;
        try {
            await api('/builder/cancel', { method: 'POST' });
            toast('Anulowano build', 'info');
        } catch (e) {
            toast(e.message || t('Błąd'), 'error');
        }
    }

    /* ═══════════════════════════════════════════
       Build Spec Tab
    ═══════════════════════════════════════════ */
    async function renderSpec() {
        blBody.innerHTML = '<div class="bl-empty"><i class="fas fa-spinner fa-spin"></i> ' + t('Ładowanie...') + '</div>';
        let spec, defaults;
        try {
            const res = await api('/builder/spec');
            spec = res.spec || {};
            const defRes = await api('/builder/spec/defaults');
            defaults = defRes.spec || {};
        } catch (e) {
            blBody.innerHTML = '<div class="bl-empty">' + t('Nie udało się pobrać konfiguracji build spec') + '</div>';
            return;
        }

        const base = spec.base || {};
        const identity = spec.identity || {};
        const partitions = spec.partitions || {};
        const buildCfg = spec.build || {};
        const security = spec.security || {};
        const packages = spec.packages || {};
        const services = spec.services || {};

        blBody.innerHTML = `
        <div class="bl-section">
            <div class="bl-section-title"><i class="fas fa-file-code"></i> ${t('Deklaratywna konfiguracja buildera')}</div>
            <div class="bl-spec-info" style="margin-bottom:12px">${t('Konfiguracja jest zapisywana w')} <code>data/build-spec.yaml</code>. ${t('Zmiany wpływają na następny build.')}</div>

            <div class="bl-spec-grid">
                <!-- Base -->
                <div class="bl-spec-card">
                    <div class="bl-spec-card-title"><i class="fas fa-cube"></i> ${t('Baza systemu')}</div>
                    <div class="bl-spec-field">
                        <label>${t('Dystrybucja')}</label>
                        <select class="bl-select" id="sp-distro">
                            <option value="debian" ${(base.distro || 'debian') === 'debian' ? 'selected' : ''}>Debian</option>
                            <option value="ubuntu" ${base.distro === 'ubuntu' ? 'selected' : ''}>Ubuntu Server LTS</option>
                        </select>
                    </div>
                    <div class="bl-spec-field">
                        <label id="sp-release-label">${t('Release')}</label>
                        <select class="bl-select" id="sp-release"></select>
                    </div>
                    <div class="bl-spec-field">
                        <label>${t('Architektura')}</label>
                        <input class="bl-input" id="sp-arch" value="${base.arch || 'amd64'}" readonly style="opacity:.6">
                    </div>
                    <div class="bl-spec-field">
                        <label>${t('Rozmiar obrazu (GB)')}</label>
                        <input class="bl-input" id="sp-imgsize" type="number" min="4" max="32" value="${base.img_size_gb || 8}">
                    </div>
                </div>

                <!-- Identity -->
                <div class="bl-spec-card">
                    <div class="bl-spec-card-title"><i class="fas fa-id-badge"></i> ${t('Tożsamość')}</div>
                    <div class="bl-spec-field">
                        <label>${t('Hostname')}</label>
                        <input class="bl-input" id="sp-hostname" value="${identity.hostname || 'ethos'}">
                    </div>
                    <div class="bl-spec-field">
                        <label>${t('Nazwa marki')}</label>
                        <input class="bl-input" id="sp-brand" value="${identity.brand_name || 'EthOS'}">
                    </div>
                    <div class="bl-spec-field">
                        <label>${t('Domyślny użytkownik')}</label>
                        <input class="bl-input" id="sp-user" value="${identity.default_user || 'nasadmin'}">
                    </div>
                    <div class="bl-spec-field">
                        <label>${t('Port NAS')}</label>
                        <input class="bl-input" id="sp-port" type="number" min="1" max="65535" value="${identity.nas_port || 9000}">
                    </div>
                </div>

                <!-- Partitions -->
                <div class="bl-spec-card">
                    <div class="bl-spec-card-title"><i class="fas fa-hdd"></i> ${t('Partycje')}</div>
                    <div class="bl-spec-field">
                        <label>${t('ESP (MB)')}</label>
                        <input class="bl-input" id="sp-esp" type="number" min="128" max="1024" value="${partitions.esp_mb || 256}">
                    </div>
                    <div class="bl-spec-field">
                        <label>SquashFS</label>
                        <select class="bl-select" id="sp-sqsh">
                            <option value="true" ${partitions.squashfs !== false ? 'selected' : ''}>${t('Włączony')}</option>
                            <option value="false" ${partitions.squashfs === false ? 'selected' : ''}>${t('Wyłączony')}</option>
                        </select>
                    </div>
                    <div class="bl-spec-field">
                        <label>dm-verity</label>
                        <select class="bl-select" id="sp-verity">
                            <option value="true" ${partitions.verity !== false ? 'selected' : ''}>${t('Włączony')}</option>
                            <option value="false" ${partitions.verity === false ? 'selected' : ''}>${t('Wyłączony')}</option>
                        </select>
                    </div>
                    <div class="bl-spec-field">
                        <label>${t('Dane (filesystem)')}</label>
                        <input class="bl-input" value="${partitions.data_type || 'btrfs'}" readonly style="opacity:.6">
                    </div>
                </div>

                <!-- Build -->
                <div class="bl-spec-card">
                    <div class="bl-spec-card-title"><i class="fas fa-cogs"></i> ${t('Parametry buildu')}</div>
                    <div class="bl-spec-field">
                        <label>${t('Kompresja zstd (poziom)')}</label>
                        <input class="bl-input" id="sp-comp" type="number" min="1" max="19" value="${buildCfg.compression_level || 3}">
                    </div>
                    <div class="bl-spec-field">
                        <label>${t('Min RAM do tmpfs (MB)')}</label>
                        <input class="bl-input" id="sp-tmpfs" type="number" min="4000" max="64000" value="${buildCfg.tmpfs_min_ram_mb || 10000}">
                    </div>
                    <div class="bl-spec-field">
                        <label>${t('Cache debootstrap')}</label>
                        <select class="bl-select" id="sp-cache-deb">
                            <option value="true" ${buildCfg.cache_debootstrap !== false ? 'selected' : ''}>${t('Włączony')}</option>
                            <option value="false" ${buildCfg.cache_debootstrap === false ? 'selected' : ''}>${t('Wyłączony')}</option>
                        </select>
                    </div>
                    <div class="bl-spec-field">
                        <label>${t('Cache apt')}</label>
                        <select class="bl-select" id="sp-cache-apt">
                            <option value="true" ${buildCfg.cache_apt !== false ? 'selected' : ''}>${t('Włączony')}</option>
                            <option value="false" ${buildCfg.cache_apt === false ? 'selected' : ''}>${t('Wyłączony')}</option>
                        </select>
                    </div>
                </div>

                <!-- Security -->
                <div class="bl-spec-card">
                    <div class="bl-spec-card-title"><i class="fas fa-shield-alt"></i> ${t('Bezpieczeństwo')}</div>
                    <div class="bl-spec-field">
                        <label>UFW</label>
                        <select class="bl-select" id="sp-ufw">
                            <option value="true" ${security.ufw_default_deny !== false ? 'selected' : ''}>${t('Włączony')}</option>
                            <option value="false" ${security.ufw_default_deny === false ? 'selected' : ''}>${t('Wyłączony')}</option>
                        </select>
                    </div>
                    <div class="bl-spec-field">
                        <label>Fail2Ban</label>
                        <select class="bl-select" id="sp-f2b">
                            <option value="true" ${security.fail2ban !== false ? 'selected' : ''}>${t('Włączony')}</option>
                            <option value="false" ${security.fail2ban === false ? 'selected' : ''}>${t('Wyłączony')}</option>
                        </select>
                    </div>
                    <div class="bl-spec-field">
                        <label>${t('SSH Password Auth')}</label>
                        <select class="bl-select" id="sp-sshpw">
                            <option value="true" ${security.ssh_password_auth !== false ? 'selected' : ''}>${t('Włączony')}</option>
                            <option value="false" ${security.ssh_password_auth === false ? 'selected' : ''}>${t('Wyłączony')}</option>
                        </select>
                    </div>
                    <div class="bl-spec-field">
                        <label>${t('Porty UFW')}</label>
                        <input class="bl-input" id="sp-ufw-ports" value="${(security.ufw_allow_ports || [9000, 22]).join(', ')}">
                    </div>
                </div>

                <!-- Services -->
                <div class="bl-spec-card">
                    <div class="bl-spec-card-title"><i class="fas fa-server"></i> ${t('Usługi')}</div>
                    <div class="bl-spec-field">
                        <label>${t('Włączone')}</label>
                        <div class="bl-spec-pkgs" id="sp-svc-enable">${(services.enable || []).join('\\n')}</div>
                    </div>
                    <div class="bl-spec-field">
                        <label>${t('Wyłączone')}</label>
                        <div class="bl-spec-pkgs" id="sp-svc-disable">${(services.disable || []).join('\\n')}</div>
                    </div>
                </div>
            </div>
        </div>

        <!-- Packages -->
        <div class="bl-section">
            <div class="bl-section-title"><i class="fas fa-cubes"></i> ${t('Pakiety')}</div>
            <div class="bl-spec-grid">
                <div class="bl-spec-card" style="grid-column:1/-1">
                    <div class="bl-spec-card-title"><i class="fas fa-box-open"></i> Debootstrap (${(packages.debootstrap || []).length} ${t('pakietów')})</div>
                    <div class="bl-spec-pkgs" id="sp-pkgs-deb">${(packages.debootstrap || []).join(', ')}</div>
                </div>
                <div class="bl-spec-card">
                    <div class="bl-spec-card-title"><i class="fas fa-plus-circle"></i> APT Extra (${(packages.apt_extra || []).length})</div>
                    <div class="bl-spec-pkgs" id="sp-pkgs-apt">${(packages.apt_extra || []).join(', ')}</div>
                </div>
                <div class="bl-spec-card">
                    <div class="bl-spec-card-title"><i class="fab fa-python"></i> Pip (${(packages.pip || []).length})</div>
                    <div class="bl-spec-pkgs" id="sp-pkgs-pip">${(packages.pip || []).join(', ')}</div>
                </div>
            </div>
        </div>

        <!-- Actions -->
        <div class="bl-spec-actions">
            <button class="bl-btn bl-btn-outline" id="sp-reset"><i class="fas fa-undo"></i> ${t('Przywróć domyślne')}</button>
            <button class="bl-btn bl-btn-green" id="sp-save"><i class="fas fa-save"></i> ${t('Zapisz konfigurację')}</button>
        </div>`;

        // Populate and manage release options based on distro
        const RELEASES = {
            debian: [{v: 'bookworm', l: 'Bookworm (12 LTS)'}, {v: 'trixie', l: 'Trixie (13)'}],
            ubuntu: [{v: 'noble', l: 'Noble Numbat (24.04 LTS)'}, {v: 'jammy', l: 'Jammy Jellyfish (22.04 LTS)'}],
        };
        function updateReleaseOptions(distro, currentRelease) {
            const sel = blBody.querySelector('#sp-release');
            sel.innerHTML = '';
            (RELEASES[distro] || RELEASES.debian).forEach(opt => {
                const o = document.createElement('option');
                o.value = opt.v;
                o.textContent = opt.l;
                if (opt.v === currentRelease) o.selected = true;
                sel.appendChild(o);
            });
            if (!sel.value) sel.selectedIndex = 0;
        }
        updateReleaseOptions(base.distro || 'debian', base.release || 'bookworm');
        blBody.querySelector('#sp-distro').onchange = (e) => {
            const defaults = {debian: 'bookworm', ubuntu: 'noble'};
            updateReleaseOptions(e.target.value, defaults[e.target.value] || 'bookworm');
        };

        // Save spec
        blBody.querySelector('#sp-save').onclick = async () => {
            const updated = {
                base: {
                    distro: blBody.querySelector('#sp-distro').value,
                    release: blBody.querySelector('#sp-release').value,
                    arch: 'amd64',
                    img_size_gb: parseInt(blBody.querySelector('#sp-imgsize').value) || 8,
                },
                identity: {
                    hostname: blBody.querySelector('#sp-hostname').value.trim() || 'ethos',
                    brand_name: blBody.querySelector('#sp-brand').value.trim() || 'EthOS',
                    default_user: blBody.querySelector('#sp-user').value.trim() || 'nasadmin',
                    nas_port: parseInt(blBody.querySelector('#sp-port').value) || 9000,
                },
                partitions: {
                    esp_mb: parseInt(blBody.querySelector('#sp-esp').value) || 256,
                    squashfs: blBody.querySelector('#sp-sqsh').value === 'true',
                    verity: blBody.querySelector('#sp-verity').value === 'true',
                },
                build: {
                    compression_level: parseInt(blBody.querySelector('#sp-comp').value) || 3,
                    tmpfs_min_ram_mb: parseInt(blBody.querySelector('#sp-tmpfs').value) || 10000,
                    cache_debootstrap: blBody.querySelector('#sp-cache-deb').value === 'true',
                    cache_apt: blBody.querySelector('#sp-cache-apt').value === 'true',
                },
                security: {
                    ufw_default_deny: blBody.querySelector('#sp-ufw').value === 'true',
                    fail2ban: blBody.querySelector('#sp-f2b').value === 'true',
                    ssh_password_auth: blBody.querySelector('#sp-sshpw').value === 'true',
                    ufw_allow_ports: blBody.querySelector('#sp-ufw-ports').value.split(',').map(p => parseInt(p.trim())).filter(Boolean),
                },
            };
            try {
                await api('/builder/spec', { method: 'PUT', body: JSON.stringify(updated) });
                toast(t('Konfiguracja zapisana'), 'success');
            } catch (e) {
                toast(e.message || t('Błąd zapisu'), 'error');
            }
        };

        // Reset to defaults
        blBody.querySelector('#sp-reset').onclick = async () => {
            if (!await confirmDialog(t('Przywrócić domyślną konfigurację?'))) return;
            try {
                await api('/builder/spec', { method: 'DELETE' });
                toast(t('Przywrócono domyślne'), 'success');
                renderSpec();
            } catch (e) {
                toast(e.message || t('Błąd'), 'error');
            }
        };
    }

    /* ─── Init ─── */
    loadInfo().then(() => checkBuildStatus());
}
