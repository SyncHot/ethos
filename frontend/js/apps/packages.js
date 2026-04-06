/* ═══════════════════════════════════════════════════════════
   EthOS — Package Manager (App Store)
   apt-get management: update, upgrade, install, remove, clean
   ═══════════════════════════════════════════════════════════ */

AppRegistry['packages'] = function (appDef) {

    const _cl = (level, msg, details) => typeof NAS !== 'undefined' && NAS.logClient
        ? NAS.logClient('packages', level, msg, details) : console.log('[packages]', msg, details || '');

    const body = document.createElement('div');
    body.className = 'pkg-app';

    body.innerHTML = `
        <div class="pkg-sidebar">
            <div class="pkg-sidebar-logo">
                <i class="fas fa-store"></i>
                <span>Pakiety</span>
            </div>
            <nav class="pkg-nav">
                <button class="pkg-nav-btn active" data-tab="overview">
                    <i class="fas fa-tachometer-alt"></i><span>${t('Przegląd')}</span>
                </button>
                <button class="pkg-nav-btn" data-tab="updates">
                    <i class="fas fa-arrow-circle-up"></i><span>Aktualizacje</span>
                    <span class="pkg-badge" id="pkg-upd-badge" style="display:none">0</span>
                </button>
                <button class="pkg-nav-btn" data-tab="installed">
                    <i class="fas fa-box"></i><span>Zainstalowane</span>
                </button>
                <button class="pkg-nav-btn" data-tab="browse">
                    <i class="fas fa-search"></i><span>${t('Przeglądaj')}</span>
                </button>
            </nav>
        </div>
        <div class="pkg-main">
            <!-- Overview -->
            <div class="pkg-tab active" id="pkg-tab-overview">
                <div class="pkg-header">
                    <h2>${t('Przegląd systemu')}</h2>
                </div>
                <div class="pkg-overview-grid" id="pkg-overview-grid">
                    <div class="pkg-stat-card">
                        <div class="pkg-stat-icon" style="background:rgba(59,130,246,0.12);color:#3b82f6">
                            <i class="fas fa-box"></i>
                        </div>
                        <div class="pkg-stat-info">
                            <span class="pkg-stat-val" id="pkg-s-installed">—</span>
                            <span class="pkg-stat-label">Zainstalowane</span>
                        </div>
                    </div>
                    <div class="pkg-stat-card">
                        <div class="pkg-stat-icon" style="background:rgba(245,158,11,0.12);color:#f59e0b">
                            <i class="fas fa-arrow-up"></i>
                        </div>
                        <div class="pkg-stat-info">
                            <span class="pkg-stat-val" id="pkg-s-upgradable">—</span>
                            <span class="pkg-stat-label">Do aktualizacji</span>
                        </div>
                    </div>
                    <div class="pkg-stat-card">
                        <div class="pkg-stat-icon" style="background:rgba(239,68,68,0.12);color:#ef4444">
                            <i class="fas fa-hdd"></i>
                        </div>
                        <div class="pkg-stat-info">
                            <span class="pkg-stat-val" id="pkg-s-cache">—</span>
                            <span class="pkg-stat-label">${t('Pamięć cache')}</span>
                        </div>
                    </div>
                    <div class="pkg-stat-card">
                        <div class="pkg-stat-icon" style="background:rgba(34,197,94,0.12);color:#22c55e">
                            <i class="fas fa-server"></i>
                        </div>
                        <div class="pkg-stat-info">
                            <span class="pkg-stat-val" id="pkg-s-os">—</span>
                            <span class="pkg-stat-label">System</span>
                        </div>
                    </div>
                </div>
                <div class="pkg-actions-row">
                    <button class="pkg-action-btn" id="pkg-btn-update">
                        <i class="fas fa-sync-alt"></i>
                        <div><strong>${t('Aktualizuj listę')}</strong><small>apt-get update</small></div>
                    </button>
                    <button class="pkg-action-btn" id="pkg-btn-upgrade">
                        <i class="fas fa-arrow-circle-up"></i>
                        <div><strong>Aktualizuj pakiety</strong><small>apt-get upgrade</small></div>
                    </button>
                    <button class="pkg-action-btn" id="pkg-btn-clean">
                        <i class="fas fa-broom"></i>
                        <div><strong>${t('Wyczyść')}</strong><small>autoremove + clean</small></div>
                    </button>
                </div>
                <div class="pkg-dpkg-warn" id="pkg-dpkg-warn" style="display:none">
                    <i class="fas fa-exclamation-triangle"></i>
                    <span>${t('dpkg został przerwany. Pakiety mogą nie działać poprawnie.')}</span>
                    <button class="pkg-action-btn small" id="pkg-btn-fix-dpkg">
                        <i class="fas fa-wrench"></i>
                        <div><strong>Napraw dpkg</strong><small>dpkg --configure -a</small></div>
                    </button>
                </div>
                <!-- Inline terminal output -->
                <div class="pkg-output-wrap" id="pkg-output-wrap" style="display:none">
                    <div class="pkg-output-header">
                        <span id="pkg-output-title">Operacja w toku…</span>
                        <button class="pkg-output-close" id="pkg-output-close"><i class="fas fa-times"></i></button>
                    </div>
                    <pre class="pkg-output" id="pkg-output"></pre>
                </div>
            </div>

            <!-- Updates -->
            <div class="pkg-tab" id="pkg-tab-updates">
                <div class="pkg-header">
                    <h2>${t('Dostępne aktualizacje')}</h2>
                    <div class="pkg-header-actions">
                        <button class="btn btn-sm" id="pkg-btn-refresh-upd"><i class="fas fa-sync-alt"></i> ${t('Odśwież')}</button>
                        <button class="btn btn-sm btn-primary" id="pkg-btn-upgrade-all"><i class="fas fa-arrow-up"></i> Aktualizuj wszystko</button>
                    </div>
                </div>
                <div id="pkg-updates-list" class="pkg-list"></div>
            </div>

            <!-- Installed -->
            <div class="pkg-tab" id="pkg-tab-installed">
                <div class="pkg-header">
                    <h2>Zainstalowane pakiety</h2>
                    <div class="pkg-header-actions">
                        <div class="pkg-search-box">
                            <i class="fas fa-search"></i>
                            <input type="text" id="pkg-installed-search" placeholder="Filtruj…">
                        </div>
                    </div>
                </div>
                <div id="pkg-installed-list" class="pkg-list"></div>
                <div class="pkg-list-status" id="pkg-installed-status"></div>
            </div>

            <!-- Browse / Search -->
            <div class="pkg-tab" id="pkg-tab-browse">
                <div class="pkg-header">
                    <h2>${t('Przeglądaj pakiety')}</h2>
                    <div class="pkg-header-actions">
                        <div class="pkg-search-box large">
                            <i class="fas fa-search"></i>
                            <input type="text" id="pkg-browse-search" placeholder="${t('Szukaj pakietów…')}">
                        </div>
                    </div>
                </div>
                <div id="pkg-browse-list" class="pkg-list"></div>
                <div class="pkg-list-status" id="pkg-browse-status"></div>
            </div>
        </div>
    `;

    createWindow('packages', {
        title: appDef.name || t('Menedżer pakietów'),
        icon: appDef.icon || 'fa-store',
        iconColor: appDef.color || '#a855f7',
        width: 1000,
        height: 650,
        content: body.outerHTML,
        onRender: init,
    });

    function init(container) {
        const root = container;

        // Tab navigation
        root.querySelectorAll('.pkg-nav-btn').forEach(btn => {
            btn.addEventListener('click', () => {
                root.querySelectorAll('.pkg-nav-btn').forEach(b => b.classList.remove('active'));
                root.querySelectorAll('.pkg-tab').forEach(t => t.classList.remove('active'));
                btn.classList.add('active');
                const tab = root.querySelector(`#pkg-tab-${btn.dataset.tab}`);
                if (tab) tab.classList.add('active');

                // Lazy-load data
                if (btn.dataset.tab === 'updates') loadUpdates();
                if (btn.dataset.tab === 'installed') loadInstalled();
            });
        });

        // Overview buttons
        root.querySelector('#pkg-btn-update').addEventListener('click', () => runStreamAction('/packages/update', 'apt-get update'));
        root.querySelector('#pkg-btn-upgrade').addEventListener('click', () => runStreamAction('/packages/upgrade', 'apt-get upgrade'));
        root.querySelector('#pkg-btn-clean').addEventListener('click', () => runStreamAction('/packages/clean', 'Czyszczenie'));
        root.querySelector('#pkg-btn-fix-dpkg').addEventListener('click', () => {
            confirmDialog(t('Naprawa dpkg może zrestartować Docker i EthOS. Kontynuować?'), 'System automatycznie wstanie ponownie po chwili.').then(ok => {
                if (ok) runStreamAction('/packages/fix-dpkg', 'dpkg --configure -a');
            });
        });
        root.querySelector('#pkg-output-close').addEventListener('click', () => {
            root.querySelector('#pkg-output-wrap').style.display = 'none';
        });

        // Updates tab buttons
        root.querySelector('#pkg-btn-refresh-upd').addEventListener('click', loadUpdates);
        root.querySelector('#pkg-btn-upgrade-all').addEventListener('click', () => runStreamAction('/packages/upgrade', 'apt-get upgrade'));

        // Installed filter
        let filterTimer = null;
        root.querySelector('#pkg-installed-search').addEventListener('input', (e) => {
            clearTimeout(filterTimer);
            filterTimer = setTimeout(() => filterInstalled(e.target.value), 200);
        });

        // Browse search
        let searchTimer = null;
        root.querySelector('#pkg-browse-search').addEventListener('input', (e) => {
            clearTimeout(searchTimer);
            searchTimer = setTimeout(() => searchPackages(e.target.value), 400);
        });

        // Load overview
        loadStats();

        // ── State ──
        let allInstalled = [];

        // ── Global task progress bridge (top stacked notifications) ──
        function _pkgTaskId(prefix) {
            return `pkg:${prefix}:${Date.now()}:${Math.random().toString(16).slice(2, 8)}`;
        }

        function _parsePercent(line) {
            if (!line) return null;
            const m = line.match(/(\d{1,3})\s*%/);
            if (!m) return null;
            const v = parseInt(m[1], 10);
            if (!isFinite(v)) return null;
            return Math.max(0, Math.min(100, v));
        }

        function _estimateEta(startTs, pct) {
            if (!startTs || !pct || pct <= 0 || pct >= 100) return null;
            const elapsed = (Date.now() - startTs) / 1000;
            const total = elapsed / (pct / 100);
            const eta = Math.max(0, total - elapsed);
            return isFinite(eta) ? eta : null;
        }

        function _taskUpsert(id, title, message, pct, startTs) {
            if (!NAS.taskProgress) return;
            NAS.taskProgress.upsert({
                id,
                source: 'Pakiety (apt)',
                title,
                message: message || '',
                percent: typeof pct === 'number' ? pct : null,
                etaSeconds: _estimateEta(startTs, pct),
                action: { app: 'packages', tab: 'overview' },
                status: 'running',
            });
        }

        function _taskFinish(id, success, message) {
            if (!NAS.taskProgress) return;
            NAS.taskProgress.finish(id, !!success, message || (success ? t('Zakończono') : t('Błąd')));
        }

        // ── Load stats ──
        async function loadStats() {
            try {
                const data = await api('/packages/stats');
                root.querySelector('#pkg-s-installed').textContent = data.installed?.toLocaleString() || '0';
                root.querySelector('#pkg-s-upgradable').textContent = data.upgradable || '0';
                root.querySelector('#pkg-s-cache').textContent = data.cache_size || '0B';
                root.querySelector('#pkg-s-os').textContent = data.os_name || 'Ubuntu';

                // dpkg health warning
                const dpkgWarn = root.querySelector('#pkg-dpkg-warn');
                if (dpkgWarn) dpkgWarn.style.display = data.dpkg_ok === false ? 'flex' : 'none';

                const badge = root.querySelector('#pkg-upd-badge');
                if (data.upgradable > 0) {
                    badge.textContent = data.upgradable;
                    badge.style.display = 'inline-flex';
                } else {
                    badge.style.display = 'none';
                }
            } catch (e) {
                console.error('Stats error:', e);
            }
        }

        // ── Stream action (SSE) ──
        async function runStreamAction(endpoint, title) {
            const wrap = root.querySelector('#pkg-output-wrap');
            const output = root.querySelector('#pkg-output');
            const titleEl = root.querySelector('#pkg-output-title');

            wrap.style.display = 'block';
            titleEl.textContent = title + ' — w toku…';
            output.textContent = '';

            // Switch to overview tab
            root.querySelectorAll('.pkg-nav-btn').forEach(b => b.classList.remove('active'));
            root.querySelectorAll('.pkg-tab').forEach(t => t.classList.remove('active'));
            root.querySelector('[data-tab="overview"]').classList.add('active');
            root.querySelector('#pkg-tab-overview').classList.add('active');

            try {
                const taskId = _pkgTaskId(endpoint.replace(/[^a-z]/gi, ''));
                const startedAt = Date.now();
                let progressPct = 5;
                _taskUpsert(taskId, title, 'Start…', progressPct, startedAt);

                const token = NAS.token || '';
                const resp = await fetch(`/api${endpoint}`, {
                    method: 'POST',
                    headers: {
                        'Authorization': `Bearer ${token}`,
                        'X-CSRFToken': NAS.csrfToken,
                        'Content-Type': 'application/json'
                    }
                });

                const reader = resp.body.getReader();
                const decoder = new TextDecoder();
                let exitCode = null;

                while (true) {
                    const { done, value } = await reader.read();
                    if (done) break;
                    const text = decoder.decode(value, { stream: true });
                    // Parse SSE
                    const lines = text.split('\n');
                    for (const line of lines) {
                        if (line.startsWith('data: ')) {
                            const content = line.slice(6);
                            if (content.startsWith('__EXIT_CODE__:')) {
                                exitCode = parseInt(content.split(':')[1]);
                            } else if (content.startsWith('__KEEPALIVE__')) {
                                /* keepalive ping */
                            } else {
                                output.textContent += content;
                                output.scrollTop = output.scrollHeight;
                                const parsed = _parsePercent(content);
                                if (typeof parsed === 'number') progressPct = Math.max(progressPct, parsed);
                                else progressPct = Math.min(95, progressPct + 0.2);
                                _taskUpsert(taskId, title, content.slice(0, 120), progressPct, startedAt);
                            }
                        }
                    }
                }

                if (exitCode === 0) {
                    titleEl.textContent = title + t(' — zakończono ✓');
                    titleEl.style.color = '#22c55e';
                    _taskFinish(taskId, true, title + ' — ' + t('zakończono'));
                } else {
                    titleEl.textContent = title + ` — ${t('błąd')} (${t('kod')} ${exitCode})`;
                    titleEl.style.color = '#ef4444';
                    _taskFinish(taskId, false, title + ` — ${t('błąd')} (${exitCode})`);
                }
            } catch (e) {
                output.textContent += `\n${t('Błąd:')} ${e.message}\n`;
                titleEl.textContent = title + t(' — błąd');
                titleEl.style.color = '#ef4444';
                const taskId = _pkgTaskId('error');
                _taskUpsert(taskId, title, e.message || t('Błąd'), 100, Date.now());
                _taskFinish(taskId, false, title + ' — ' + t('błąd'));
            }

            // Refresh stats after action
            setTimeout(() => {
                titleEl.style.color = '';
                loadStats();
            }, 1000);
        }

        // ── Install / Remove via SSE ──
        async function runPackageAction(endpoint, packages, actionTitle) {
            const wrap = root.querySelector('#pkg-output-wrap');
            const output = root.querySelector('#pkg-output');
            const titleEl = root.querySelector('#pkg-output-title');

            wrap.style.display = 'block';
            titleEl.textContent = actionTitle + ' — w toku…';
            titleEl.style.color = '';
            output.textContent = '';

            // Switch to overview tab
            root.querySelectorAll('.pkg-nav-btn').forEach(b => b.classList.remove('active'));
            root.querySelectorAll('.pkg-tab').forEach(t => t.classList.remove('active'));
            root.querySelector('[data-tab="overview"]').classList.add('active');
            root.querySelector('#pkg-tab-overview').classList.add('active');

            try {
                const taskId = _pkgTaskId(endpoint.replace(/[^a-z]/gi, ''));
                const startedAt = Date.now();
                let progressPct = 5;
                _taskUpsert(taskId, actionTitle, packages.join(', '), progressPct, startedAt);

                const token = NAS.token || '';
                const resp = await fetch(`/api${endpoint}`, {
                    method: 'POST',
                    headers: {
                        'Authorization': `Bearer ${token}`,
                        'X-CSRFToken': NAS.csrfToken,
                        'Content-Type': 'application/json'
                    },
                    body: JSON.stringify({ packages: packages })
                });

                const reader = resp.body.getReader();
                const decoder = new TextDecoder();
                let exitCode = null;

                while (true) {
                    const { done, value } = await reader.read();
                    if (done) break;
                    const text = decoder.decode(value, { stream: true });
                    const lines = text.split('\n');
                    for (const line of lines) {
                        if (line.startsWith('data: ')) {
                            const content = line.slice(6);
                            if (content.startsWith('__EXIT_CODE__:')) {
                                exitCode = parseInt(content.split(':')[1]);
                            } else if (content.startsWith('__KEEPALIVE__')) {
                                /* keepalive ping */
                            } else {
                                output.textContent += content;
                                output.scrollTop = output.scrollHeight;
                                const parsed = _parsePercent(content);
                                if (typeof parsed === 'number') progressPct = Math.max(progressPct, parsed);
                                else progressPct = Math.min(95, progressPct + 0.2);
                                _taskUpsert(taskId, actionTitle, content.slice(0, 120), progressPct, startedAt);
                            }
                        }
                    }
                }

                if (exitCode === 0) {
                    titleEl.textContent = actionTitle + t(' — zakończono ✓');
                    titleEl.style.color = '#22c55e';
                    toast(actionTitle + t(' zakończono'), 'success');
                    _taskFinish(taskId, true, actionTitle + ' — ' + t('zakończono'));
                } else {
                    titleEl.textContent = actionTitle + ` — ${t('błąd')} (${t('kod')} ${exitCode})`;
                    titleEl.style.color = '#ef4444';
                    _taskFinish(taskId, false, actionTitle + ` — ${t('błąd')} (${exitCode})`);
                }
            } catch (e) {
                output.textContent += `\n${t('Błąd:')} ${e.message}\n`;
                titleEl.textContent = actionTitle + t(' — błąd');
                titleEl.style.color = '#ef4444';
                const taskId = _pkgTaskId('error');
                _taskUpsert(taskId, actionTitle, e.message || t('Błąd'), 100, Date.now());
                _taskFinish(taskId, false, actionTitle + ' — ' + t('błąd'));
            }

            setTimeout(() => {
                titleEl.style.color = '';
                loadStats();
            }, 1000);
        }

        // ── Updates tab ──
        async function loadUpdates() {
            const list = root.querySelector('#pkg-updates-list');
            list.innerHTML = `<div class="pkg-loading"><i class="fas fa-spinner fa-spin"></i> ${t('Ładowanie…')}</div>`;
            try {
                const data = await api('/packages/upgradable');
                if (!data.length) {
                    list.innerHTML = `
                        <div class="pkg-empty">
                            <i class="fas fa-check-circle"></i>
                            <span>System jest aktualny!</span>
                        </div>`;
                    return;
                }
                list.innerHTML = data.map(p => `
                    <div class="pkg-item">
                        <div class="pkg-item-icon upd"><i class="fas fa-arrow-circle-up"></i></div>
                        <div class="pkg-item-info">
                            <span class="pkg-item-name">${esc(p.name)}</span>
                            <span class="pkg-item-meta">${esc(p.old_version)} → ${esc(p.new_version)}</span>
                        </div>
                        <button class="pkg-item-btn install" data-pkg="${esc(p.name)}" title="Aktualizuj">
                            <i class="fas fa-arrow-up"></i>
                        </button>
                    </div>
                `).join('');

                list.querySelectorAll('.pkg-item-btn.install').forEach(btn => {
                    btn.addEventListener('click', () => {
                        const pkg = btn.dataset.pkg;
                        runPackageAction('/packages/install', [pkg], `Aktualizacja ${pkg}`);
                    });
                });
            } catch (e) {
                list.innerHTML = `<div class="pkg-empty"><i class="fas fa-exclamation-circle"></i> ${t('Błąd:')} ${e.message}</div>`;
            }
        }

        // ── Installed tab ──
        async function loadInstalled() {
            const list = root.querySelector('#pkg-installed-list');
            const status = root.querySelector('#pkg-installed-status');
            list.innerHTML = `<div class="pkg-loading"><i class="fas fa-spinner fa-spin"></i> ${t('Ładowanie…')}</div>`;
            try {
                allInstalled = await api('/packages/installed');
                renderInstalled(allInstalled);
                status.textContent = `${allInstalled.length} ${t('pakietów')}`;
            } catch (e) {
                list.innerHTML = `<div class="pkg-empty"><i class="fas fa-exclamation-circle"></i> ${t('Błąd:')} ${e.message}</div>`;
            }
        }

        function renderInstalled(pkgs) {
            const list = root.querySelector('#pkg-installed-list');
            if (!pkgs.length) {
                list.innerHTML = `<div class="pkg-empty"><i class="fas fa-box-open"></i> ${t('Brak pakietów')}</div>`;
                return;
            }

            // Show max 200 for performance
            const display = pkgs.slice(0, 200);
            list.innerHTML = display.map(p => `
                <div class="pkg-item">
                    <div class="pkg-item-icon inst"><i class="fas fa-cube"></i></div>
                    <div class="pkg-item-info">
                        <span class="pkg-item-name">${esc(p.name)}</span>
                        <span class="pkg-item-meta">${esc(p.version)} · ${formatSize(p.size)}</span>
                        <span class="pkg-item-desc">${esc(p.description || '')}</span>
                    </div>
                    <button class="pkg-item-btn remove" data-pkg="${esc(p.name)}" title="${t('Usuń')}">
                        <i class="fas fa-trash"></i>
                    </button>
                </div>
            `).join('') + (pkgs.length > 200 ? `<div class="pkg-list-more">${t('Wyświetlono')} 200 ${t('z')} ${pkgs.length} — ${t('użyj filtra')}</div>` : '');

            list.querySelectorAll('.pkg-item-btn.remove').forEach(btn => {
                btn.addEventListener('click', async () => {
                    const pkg = btn.dataset.pkg;
                    const ok = await confirmDialog(t('Usunąć pakiet?'), `${t('Czy na pewno chcesz usunąć')} „${pkg}"?`);
                    if (ok) runPackageAction('/packages/remove', [pkg], `Usuwanie ${pkg}`);
                });
            });
        }

        function filterInstalled(q) {
            const filtered = q
                ? allInstalled.filter(p => p.name.includes(q.toLowerCase()) || (p.description || '').toLowerCase().includes(q.toLowerCase()))
                : allInstalled;
            renderInstalled(filtered);
            root.querySelector('#pkg-installed-status').textContent = q
                ? `${filtered.length} ${t('z')} ${allInstalled.length} ${t('pakietów')}`
                : `${allInstalled.length} ${t('pakietów')}`;
        }

        // ── Browse / Search tab ──
        async function searchPackages(q) {
            const list = root.querySelector('#pkg-browse-list');
            const status = root.querySelector('#pkg-browse-status');
            if (!q || q.length < 2) {
                list.innerHTML = '<div class="pkg-empty"><i class="fas fa-search"></i> Wpisz co najmniej 2 znaki</div>';
                status.textContent = '';
                return;
            }
            list.innerHTML = '<div class="pkg-loading"><i class="fas fa-spinner fa-spin"></i> Szukanie…</div>';
            try {
                const data = await api(`/packages/search?q=${encodeURIComponent(q)}`);
                status.textContent = `${data.length} ${t('wyników')}`;
                if (!data.length) {
                    list.innerHTML = `<div class="pkg-empty"><i class="fas fa-box-open"></i> ${t('Brak wyników')}</div>`;
                    return;
                }
                list.innerHTML = data.map(p => `
                    <div class="pkg-item">
                        <div class="pkg-item-icon ${p.installed ? 'inst' : 'avail'}">
                            <i class="fas ${p.installed ? 'fa-check-circle' : 'fa-cube'}"></i>
                        </div>
                        <div class="pkg-item-info">
                            <span class="pkg-item-name">${esc(p.name)}</span>
                            <span class="pkg-item-desc">${esc(p.description || '')}</span>
                        </div>
                        ${p.installed
                            ? `<button class="pkg-item-btn remove" data-pkg="${esc(p.name)}" title="${t('Usuń')}"><i class="fas fa-trash"></i></button>`
                            : `<button class="pkg-item-btn install" data-pkg="${esc(p.name)}" title="Zainstaluj"><i class="fas fa-download"></i></button>`
                        }
                    </div>
                `).join('');

                list.querySelectorAll('.pkg-item-btn.install').forEach(btn => {
                    btn.addEventListener('click', () => {
                        runPackageAction('/packages/install', [btn.dataset.pkg], `Instalacja ${btn.dataset.pkg}`);
                    });
                });
                list.querySelectorAll('.pkg-item-btn.remove').forEach(btn => {
                    btn.addEventListener('click', async () => {
                        const ok = await confirmDialog(t('Usunąć pakiet?'), `${t('Czy na pewno chcesz usunąć')} „${btn.dataset.pkg}"?`);
                        if (ok) runPackageAction('/packages/remove', [btn.dataset.pkg], `Usuwanie ${btn.dataset.pkg}`);
                    });
                });
            } catch (e) {
                list.innerHTML = `<div class="pkg-empty"><i class="fas fa-exclamation-circle"></i> ${t('Błąd:')} ${e.message}</div>`;
            }
        }

        // ── Helpers ──
        function esc(s) {
            const d = document.createElement('div');
            d.textContent = s;
            return d.innerHTML;
        }

        function formatSize(bytes) {
            if (!bytes || bytes <= 0) return '—';
            const units = ['B', 'KB', 'MB', 'GB'];
            let i = 0;
            let v = bytes;
            while (v >= 1024 && i < units.length - 1) { v /= 1024; i++; }
            return v.toFixed(i === 0 ? 0 : 1) + ' ' + units[i];
        }
    }
};
