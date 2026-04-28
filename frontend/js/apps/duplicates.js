/* ═══════════════════════════════════════════════════════════
   ${t('EthOS — Duplikaty zdjęć (standalone app)')}
   ═══════════════════════════════════════════════════════════ */

AppRegistry['duplicates'] = function (appDef, launchOpts) {
    // launchOpts: { scanPath: '/some/folder' } — optional, from file manager context menu
    const initialScanPath = launchOpts?.scanPath || null;
    const forceScan = launchOpts?.forceScan || false;

    createWindow('duplicates', {
        title: t('Duplikaty zdjęć'),
        icon: appDef.icon,
        iconColor: appDef.color,
        width: 1050,
        height: 650,
        onRender: (body) => renderDupApp(body),
    });

    function renderDupApp(body) {
        body.innerHTML = `
            <div class="dup-app" style="display:flex;flex-direction:column;height:100%;overflow:hidden;">
                <div class="dup-app-statusbar" id="dup-statusbar" style="padding:4px 12px;font-size:11px;color:var(--text-muted);border-top:1px solid var(--border);background:var(--bg-secondary);flex-shrink:0;">
                    ${t('Duplikaty zdjęć — gotowy')}
                </div>
                <div class="dup-app-content" id="dup-content" style="flex:1;overflow:auto;"></div>
            </div>
        `;

        const content = body.querySelector('#dup-content');
        const statusbar = body.querySelector('#dup-statusbar');

        // If launched with a specific path to scan, start immediately
        if (forceScan && initialScanPath) {
            _startScanFromPath(initialScanPath);
            return;
        }

        // Check current scan state via API
        api('/files/duplicates/status').then(status => {
            if (status.running) {
                _showScanning(true);
            } else if (status.found_groups > 0 && !status.error) {
                _showResults();
            } else {
                _showConfig();
            }
        }).catch(() => _showConfig());

        // ─── Helper: start scan from specific path ───
        async function _startScanFromPath(folderPath) {
            try {
                const res = await api('/files/duplicates/scan', {
                    method: 'POST',
                    body: { paths: [folderPath], mode: 'both', threshold: 8 }
                });
                if (res.error) {
                    if (res.error.includes(t('już trwa'))) {
                        _showScanning(true);
                    } else {
                        toast(res.error, 'error');
                        _showConfig();
                    }
                    return;
                }
                _showScanning(false);
            } catch (e) {
                toast(e.message || t('Błąd uruchamiania skanowania'), 'error');
                _showConfig();
            }
        }

        // ─── Preview window ───
        // items can have _groupIdx and _groupType for cross-group navigation
        function _openDupPreview(items, startIdx, onDelete) {
            const imageExts = ['jpg', 'jpeg', 'png', 'gif', 'bmp', 'webp', 'svg', 'ico'];
            let mediaFiles = items.filter(f => {
                const ext = f.name.split('.').pop().toLowerCase();
                return imageExts.includes(ext);
            });
            if (!mediaFiles.length) return;
            let currentIdx = startIdx != null ? Math.min(startIdx, mediaFiles.length - 1) : 0;
            const winId = 'dup-preview';

            // Count total groups for group badge
            const _totalGroups = (() => {
                const seen = new Set();
                mediaFiles.forEach(f => { if (f._groupIdx != null) seen.add(f._groupIdx); });
                return seen.size;
            })();

            function renderMedia(bodyEl) {
                const file = mediaFiles[currentIdx];
                if (!file) return;
                const src = `/api/files/preview?path=${encodeURIComponent(file.path)}`;
                const winData = WM.windows.get(winId);
                if (winData) {
                    const titleSpan = winData.el.querySelector('.window-title span');
                    if (titleSpan) titleSpan.textContent = file.name;
                }
                // Group badge (when navigating across groups)
                let groupBadge = '';
                if (file._groupIdx != null && _totalGroups > 1) {
                    const gType = file._groupType === 'exact' ? 'Identyczne' : file._groupType === 'similar' ? 'Podobne' : '';
                    const gColor = file._groupType === 'exact' ? '#38bdf8' : '#a78bfa';
                    groupBadge = `<span style="background:${gColor};color:#000;padding:2px 8px;border-radius:8px;font-size:11px;font-weight:600;margin-right:8px;">Grupa ${file._groupIdx + 1}/${_totalGroups}${gType ? ' · ' + gType : ''}</span>`;
                }
                bodyEl.innerHTML = `
                    <div class="mv-container">
                        <div class="mv-content"><img class="mv-media mv-img" src="${src}" alt="${file.name}" draggable="false"></div>
                        <div class="mv-overlay mv-nav-left" title="Poprzedni"><i class="fas fa-chevron-left"></i></div>
                        <div class="mv-overlay mv-nav-right" title="${t('Następny')}"><i class="fas fa-chevron-right"></i></div>
                        <div class="mv-topbar">
                            ${groupBadge}<span class="mv-counter">${currentIdx + 1} / ${mediaFiles.length}</span>
                            <span class="mv-filename">${file.name}</span>
                            <div class="mv-actions">
                                <button class="mv-btn" id="mv-delete" title="Do kosza (Delete)"><i class="fas fa-trash"></i></button>
                                <button class="mv-btn" id="mv-close" title="Zamknij (Esc)"><i class="fas fa-times"></i></button>
                            </div>
                        </div>
                        <div class="mv-bottombar">
                            <button class="mv-nav-btn" id="mv-prev" ${currentIdx <= 0 ? 'disabled' : ''}><i class="fas fa-arrow-left"></i> ${t('Poprzedni')}</button>
                            <span class="mv-info">${formatBytes(file.size)} · <span style="opacity:.7">${file.path}</span></span>
                            <button class="mv-nav-btn" id="mv-next" ${currentIdx >= mediaFiles.length - 1 ? 'disabled' : ''}>${t('Następny')} <i class="fas fa-arrow-right"></i></button>
                        </div>
                    </div>
                `;
                bodyEl.querySelector('.mv-nav-left').addEventListener('click', goPrev);
                bodyEl.querySelector('.mv-nav-right').addEventListener('click', goNext);
                bodyEl.querySelector('#mv-prev').addEventListener('click', goPrev);
                bodyEl.querySelector('#mv-next').addEventListener('click', goNext);
                bodyEl.querySelector('#mv-delete').addEventListener('click', deleteCurrent);
                bodyEl.querySelector('#mv-close').addEventListener('click', () => closeWindow(winId));
            }

            function goPrev() { if (currentIdx > 0) { currentIdx--; const b = document.getElementById('win-body-' + winId); if (b) renderMedia(b); } }
            function goNext() { if (currentIdx < mediaFiles.length - 1) { currentIdx++; const b = document.getElementById('win-body-' + winId); if (b) renderMedia(b); } }

            async function deleteCurrent() {
                const file = mediaFiles[currentIdx];
                if (!file) return;
                try {
                    await api('/files/delete', { method: 'DELETE', body: { paths: [file.path] } });
                    toast(`Przeniesiono do kosza: "${file.name}"`, 'success');
                    mediaFiles.splice(currentIdx, 1);
                    if (onDelete) onDelete(file);
                    if (mediaFiles.length === 0) { closeWindow(winId); return; }
                    if (currentIdx >= mediaFiles.length) currentIdx = mediaFiles.length - 1;
                    const b = document.getElementById('win-body-' + winId);
                    if (b) renderMedia(b);
                } catch { toast(t('Błąd usuwania'), 'error'); }
            }

            function onKeyDown(e) {
                const winData = WM.windows.get(winId);
                if (!winData || WM.activeId !== winId) return;
                if (e.key === 'ArrowLeft') { e.preventDefault(); goPrev(); }
                else if (e.key === 'ArrowRight') { e.preventDefault(); goNext(); }
                else if (e.key === 'Delete') { e.preventDefault(); deleteCurrent(); }
                else if (e.key === 'Escape') { e.preventDefault(); closeWindow(winId); }
            }
            document.addEventListener('keydown', onKeyDown);

            if (WM.windows.has(winId)) closeWindow(winId);
            createWindow(winId, {
                title: mediaFiles[currentIdx].name,
                icon: 'fa-clone',
                iconColor: '#f59e0b',
                width: 900, height: 650, singleton: false,
                onRender: (bodyEl) => renderMedia(bodyEl),
                onClose: () => document.removeEventListener('keydown', onKeyDown),
            });
            if (window.innerWidth <= 768) toggleMaximize(winId);
        }

        // ─── Config screen ───
        function _showConfig() {
            const dupFolders = initialScanPath ? [initialScanPath] : ['/home'];

            function _renderFolderList() {
                const container = content.querySelector('#dup-folders');
                if (!container) return;
                container.innerHTML = dupFolders.map((f, i) => `
                    <div class="fm-dup-folder-tag">
                        <i class="fas fa-folder"></i> ${f}
                        <button class="fm-dup-folder-remove" data-idx="${i}" title="${t('Usuń')}"><i class="fas fa-times"></i></button>
                    </div>
                `).join('') || '<span class="fm-dup-hint">' + t('Dodaj przynajmniej jeden folder') + '</span>';
                container.querySelectorAll('.fm-dup-folder-remove').forEach(btn => {
                    btn.addEventListener('click', () => {
                        dupFolders.splice(parseInt(btn.dataset.idx), 1);
                        _renderFolderList();
                    });
                });
            }

            content.innerHTML = `
                <div class="fm-dup-start">
                    <div class="fm-dup-start-icon"><i class="fas fa-clone"></i></div>
                    <h3>${t('Znajdź duplikaty zdjęć')}</h3>
                    <p>${t('Skanuj wybrane foldery w poszukiwaniu identycznych lub wizualnie podobnych zdjęć.')}</p>
                    <div class="fm-dup-options">
                        <label class="fm-dup-label">Foldery do skanowania:</label>
                        <div class="fm-dup-folder-list" id="dup-folders"></div>
                        <div class="fm-dup-folder-add-row">
                            <input type="text" class="fm-dup-path-input" id="dup-path-input" placeholder="${t('/home/nasadmin/Zdjęcia')}" value="">
                            <button class="fm-dup-btn" id="dup-add-path" title="Dodaj folder"><i class="fas fa-plus"></i> Dodaj</button>
                        </div>
                        <div class="fm-dup-quick-paths">
                            <span class="fm-dup-hint">${t('Szybki wybór:')}</span>
                            <button class="fm-dup-quick-btn" data-qpath="/home">/home</button>
                            <button class="fm-dup-quick-btn" data-qpath="/media">/media</button>
                        </div>
                        <label class="fm-dup-label">Tryb:</label>
                        <select class="fm-dup-select" id="dup-mode">
                            <option value="both">Identyczne + Podobne</option>
                            <option value="exact">Tylko identyczne (SHA256)</option>
                            <option value="similar">Tylko wizualnie podobne</option>
                        </select>
                        <label class="fm-dup-label">${t('Czułość podobieństwa:')}</label>
                        <div class="fm-dup-range-row">
                            <input type="range" min="2" max="16" value="8" id="dup-threshold" class="fm-dup-range">
                            <span id="dup-threshold-val">8</span>
                        </div>
                        <p class="fm-dup-hint">${t('Niższa = ściślejsze dopasowanie, wyższa = więcej wyników')}</p>
                    </div>
                    <button class="fm-dup-scan-btn" id="dup-start-btn"><i class="fas fa-search"></i> Rozpocznij skanowanie</button>
                </div>
            `;

            _renderFolderList();
            statusbar.textContent = t('Duplikaty zdjęć — wybierz foldery i uruchom skanowanie');

            content.querySelector('#dup-add-path')?.addEventListener('click', () => {
                const input = content.querySelector('#dup-path-input');
                const val = (input.value || '').trim();
                if (val && !dupFolders.includes(val)) {
                    dupFolders.push(val);
                    input.value = '';
                    _renderFolderList();
                }
            });
            content.querySelector('#dup-path-input')?.addEventListener('keydown', (e) => {
                if (e.key === 'Enter') { e.preventDefault(); content.querySelector('#dup-add-path')?.click(); }
            });
            content.querySelectorAll('.fm-dup-quick-btn').forEach(btn => {
                btn.addEventListener('click', () => {
                    const qp = btn.dataset.qpath;
                    if (!dupFolders.includes(qp)) { dupFolders.push(qp); _renderFolderList(); }
                });
            });

            const thresholdRange = content.querySelector('#dup-threshold');
            const thresholdVal = content.querySelector('#dup-threshold-val');
            thresholdRange?.addEventListener('input', () => { thresholdVal.textContent = thresholdRange.value; });

            content.querySelector('#dup-start-btn')?.addEventListener('click', async () => {
                if (!dupFolders.length) { toast('Dodaj przynajmniej jeden folder', 'warning'); return; }
                const mode = content.querySelector('#dup-mode').value;
                const threshold = parseInt(thresholdRange.value);
                try {
                    await api('/files/duplicates/scan', { method: 'POST', body: { paths: dupFolders, mode, threshold } });
                    _showScanning(false);
                } catch (e) {
                    toast(e.message || t('Błąd uruchamiania skanowania'), 'error');
                }
            });
        }

        // ─── Scanning view ───
        function _showScanning(resuming) {
            if (body._dupCleanup) { body._dupCleanup(); body._dupCleanup = null; }

            content.innerHTML = `
                <div class="fm-dup-live">
                    <div class="fm-dup-live-header" id="dup-live-header">
                        <div class="fm-dup-live-progress">
                            <div class="fm-dup-spinner"><i class="fas fa-spinner fa-spin"></i></div>
                            <div class="fm-dup-live-info">
                                <h3 id="dup-scan-phase">Rozpoczynanie skanowania…</h3>
                                <div class="fm-dup-progress-bar">
                                    <div class="fm-dup-progress-fill" id="dup-progress-fill" style="width:0%"></div>
                                </div>
                                <p class="fm-dup-scan-detail" id="dup-scan-detail">${t('Proszę czekać…')}</p>
                            </div>
                        </div>
                        <div class="fm-dup-live-actions">
                            <span class="fm-dup-found-count" id="dup-found-count">0 grup znalezionych</span>
                            <button class="fm-dup-btn fm-dup-btn-danger" id="dup-cancel-btn"><i class="fas fa-stop"></i> Anuluj</button>
                        </div>
                    </div>
                    <div class="fm-dup-live-results" id="dup-live-results">
                        <div class="fm-dup-results-actions" id="dup-partial-actions" style="display:none">
                            <button class="fm-dup-btn" id="dup-autoselect-live"><i class="fas fa-magic"></i> Auto-zaznacz duplikaty</button>
                            <button class="fm-dup-btn fm-dup-btn-danger" id="dup-trash-live"><i class="fas fa-trash"></i> Do kosza zaznaczone</button>
                        </div>
                        <div class="fm-dup-groups" id="dup-groups"></div>
                    </div>
                </div>
            `;
            statusbar.textContent = 'Skanowanie w toku…';

            let liveGroups = [];

            content.querySelector('#dup-cancel-btn')?.addEventListener('click', async () => {
                try {
                    await api('/files/duplicates/cancel', { method: 'POST' });
                    toast('Anulowanie skanowania…', 'info');
                } catch (e) { toast(e.message || t('Nie udało się anulować'), 'error'); }
            });

            content.querySelector('#dup-autoselect-live')?.addEventListener('click', () => {
                content.querySelectorAll('.fm-dup-file-cb input[type="checkbox"]').forEach(cb => {
                    cb.checked = parseInt(cb.dataset.idx) !== 0;
                });
                _updateLiveTrashBtn();
            });

            content.querySelector('#dup-trash-live')?.addEventListener('click', async () => {
                const checked = [...content.querySelectorAll('.fm-dup-file-cb input:checked')];
                if (!checked.length) { toast(t('Zaznacz pliki do usunięcia'), 'warning'); return; }
                const paths = checked.map(cb => {
                    const gi = parseInt(cb.dataset.group);
                    const fi = parseInt(cb.dataset.idx);
                    return liveGroups[gi]?.items[fi]?.path;
                }).filter(Boolean);
                const sure = await confirmDialog(t('Do kosza'), t('Przenieść') + ' ' + paths.length + ' ' + t('duplikatów do kosza?'));
                if (!sure) return;
                try {
                    await api('/files/delete', { method: 'DELETE', body: { paths } });
                    toast(`Przeniesiono ${paths.length} ${t('duplikatów do kosza')}`, 'success');
                    checked.forEach(cb => {
                        const fileEl = cb.closest('.fm-dup-file');
                        if (fileEl) fileEl.remove();
                    });
                    content.querySelectorAll('.fm-dup-group').forEach(gEl => {
                        if (gEl.querySelectorAll('.fm-dup-file').length < 2) gEl.remove();
                    });
                } catch { toast(t('Błąd usuwania'), 'error'); }
            });

            function _updateLiveTrashBtn() {
                const count = content.querySelectorAll('.fm-dup-file-cb input:checked').length;
                const btn = content.querySelector('#dup-trash-live');
                if (btn) btn.innerHTML = `<i class="fas fa-trash"></i> Do kosza (${count})`;
            }

            function _appendGroupToDOM(group, gi) {
                const groupsContainer = content.querySelector('#dup-groups');
                if (!groupsContainer) return;
                const actionsBar = content.querySelector('#dup-partial-actions');
                if (actionsBar) actionsBar.style.display = '';

                const div = document.createElement('div');
                div.className = 'fm-dup-group';
                div.dataset.group = gi;
                div.innerHTML = `
                    <div class="fm-dup-group-header">
                        <span class="fm-dup-group-badge ${group.type === 'exact' ? 'fm-dup-exact' : 'fm-dup-similar'}">
                            ${group.type === 'exact' ? '<i class="fas fa-equals"></i> Identyczne' : '<i class="fas fa-eye"></i> Podobne'}
                        </span>
                        <span class="fm-dup-group-count">${group.items.length} ${t('plików')} · ${formatBytes(group.items.reduce((s,f) => s + f.size, 0))}</span>
                        <button class="fm-dup-btn fm-dup-ignore-btn" data-group="${gi}" title="${t('Ignoruj tę grupę')}"><i class="fas fa-eye-slash"></i> ${t('Ignoruj')}</button>
                    </div>
                    <div class="fm-dup-group-items">
                        ${group.items.map((file, fi) => {
                            const isFirst = fi === 0;
                            const thumbSrc = '/api/files/preview?path=' + encodeURIComponent(file.path) + '&w=80&h=80';
                            return `
                                <div class="fm-dup-file${isFirst ? ' fm-dup-original' : ''}" data-group="${gi}" data-idx="${fi}" data-path="${file.path}">
                                    <label class="fm-dup-file-cb">
                                        <input type="checkbox" data-group="${gi}" data-idx="${fi}">
                                        <span class="fm-cb-custom"></span>
                                    </label>
                                    <img src="${thumbSrc}" class="fm-dup-thumb" alt="" loading="lazy">
                                    <div class="fm-dup-file-info">
                                        <div class="fm-dup-file-name">${file.name}${isFirst ? ` <span class="fm-dup-keep-badge">${t('oryginał')}</span>` : ''}</div>
                                        <div class="fm-dup-file-meta">
                                            <span title="${file.path}"><i class="fas fa-folder-open"></i> ${file.path}</span>
                                            <span>${formatBytes(file.size)}</span>
                                            <span><i class="fas fa-calendar"></i> ${formatDate(file.modified)}</span>
                                        </div>
                                    </div>
                                </div>
                            `;
                        }).join('')}
                    </div>
                `;
                groupsContainer.appendChild(div);
                div.querySelectorAll('.fm-dup-file-cb input').forEach(cb => {
                    cb.addEventListener('change', () => _updateLiveTrashBtn());
                });
                div.querySelectorAll('.fm-dup-thumb').forEach(thumb => {
                    thumb.style.cursor = 'pointer';
                    thumb.addEventListener('click', (e) => {
                        e.stopPropagation();
                        const fileEl = thumb.closest('.fm-dup-file');
                        const fi = parseInt(fileEl?.dataset.idx || 0);
                        // Flatten all groups for cross-group navigation
                        const allItems = [];
                        let globalIdx = 0;
                        liveGroups.forEach((g, gIdx) => {
                            g.items.forEach((f, fIdx) => {
                                if (gIdx === gi && fIdx === fi) globalIdx = allItems.length;
                                allItems.push({ ...f, _groupIdx: gIdx, _groupType: g.type });
                            });
                        });
                        _openDupPreview(allItems, globalIdx, (deleted) => {
                            const gIdx = deleted._groupIdx;
                            const groupDiv = content.querySelector(`.fm-dup-group[data-group="${gIdx}"]`);
                            if (groupDiv) {
                                const el = groupDiv.querySelector(`.fm-dup-file[data-path="${CSS.escape(deleted.path)}"]`);
                                if (el) el.remove();
                                if (groupDiv.querySelectorAll('.fm-dup-file').length < 2) groupDiv.remove();
                            }
                        });
                    });
                });
                div.querySelector('.fm-dup-ignore-btn')?.addEventListener('click', async () => {
                    try {
                        await api('/files/duplicates/ignore', { method: 'POST', body: { groups: [group] } });
                        toast(t('Grupa oznaczona jako ignorowana'), 'success');
                        div.remove();
                    } catch { toast(t('Błąd ignorowania'), 'error'); }
                });
            }

            // Resume: load existing state
            if (resuming) {
                api('/files/duplicates/status').then(status => {
                    const phaseEl = content.querySelector('#dup-scan-phase');
                    const fillEl = content.querySelector('#dup-progress-fill');
                    const detailEl = content.querySelector('#dup-scan-detail');
                    if (phaseEl) phaseEl.textContent = status.phase || 'Skanowanie…';
                    if (fillEl && status.total > 0) fillEl.style.width = Math.round(status.scanned / status.total * 100) + '%';
                    if (detailEl) detailEl.textContent = `${status.scanned || 0} / ${status.total || '?'}`;
                }).catch(() => {});
                api('/files/duplicates/results').then(data => {
                    const groups = data.groups || [];
                    groups.forEach((group, i) => {
                        liveGroups.push(group);
                        _appendGroupToDOM(group, i);
                    });
                    const countEl = content.querySelector('#dup-found-count');
                    if (countEl) countEl.textContent = `${liveGroups.length} grup znalezionych`;
                    statusbar.textContent = t('Skanowanie…') + ' ' + liveGroups.length + ' ' + t('grup duplikatów znalezionych');
                }).catch(() => {});
            }

            // Socket.IO listeners (view-local)
            const onProgress = (data) => {
                const phaseEl = content.querySelector('#dup-scan-phase');
                const fillEl = content.querySelector('#dup-progress-fill');
                const detailEl = content.querySelector('#dup-scan-detail');
                if (!phaseEl) return;
                phaseEl.textContent = data.phase || '';
                if (fillEl && data.total > 0) fillEl.style.width = Math.round(data.scanned / data.total * 100) + '%';
                if (detailEl) detailEl.textContent = `${data.scanned || 0} / ${data.total || '?'}`;
            };

            const onNewGroup = (data) => {
                if (!content.querySelector('#dup-groups')) return;
                const group = data.group;
                liveGroups.push(group);
                _appendGroupToDOM(group, liveGroups.length - 1);
                const countEl = content.querySelector('#dup-found-count');
                if (countEl) countEl.textContent = `${liveGroups.length} grup znalezionych`;
                statusbar.textContent = t('Skanowanie…') + ' ' + liveGroups.length + ' ' + t('grup duplikatów znalezionych');
            };

            const _cleanup = () => {
                NAS.socket?.off('dup_progress', onProgress);
                NAS.socket?.off('dup_complete', onComplete);
                NAS.socket?.off('dup_error', onError);
                NAS.socket?.off('dup_new_group', onNewGroup);
                NAS.socket?.off('dup_cancelled', onCancelled);
                body._dupCleanup = null;
            };

            const onComplete = (data) => {
                _cleanup();
                const header = content.querySelector('#dup-live-header');
                if (!header) return;
                header.innerHTML = `
                    <div class="fm-dup-results-info">
                        <i class="fas fa-check-circle" style="color:var(--accent)"></i>
                        <strong>${data.groups}</strong> ${t('grup duplikatów &middot;')}
                        <strong>${data.duplicates}</strong> ${t('nadmiarowych plików &middot;')}
                        <strong>${formatBytes(data.size)}</strong> do odzyskania
                    </div>
                    <div class="fm-dup-live-actions">
                        <button class="fm-dup-btn" id="dup-rescan2"><i class="fas fa-redo"></i> Skanuj ponownie</button>
                    </div>
                `;
                header.querySelector('#dup-rescan2')?.addEventListener('click', () => _showConfig());
                statusbar.textContent = t('Duplikaty:') + ' ' + data.groups + ' ' + t('grup,') + ' ' + data.duplicates + ' ' + t('nadmiarowych plików') + ' (' + formatBytes(data.size) + ')';
            };

            const onCancelled = (data) => {
                _cleanup();
                const header = content.querySelector('#dup-live-header');
                if (!header) return;
                header.innerHTML = `
                    <div class="fm-dup-results-info">
                        <i class="fas fa-stop-circle" style="color:#f59e0b"></i>
                        Skanowanie anulowane &middot; <strong>${data.groups || liveGroups.length}</strong> grup znalezionych przed anulowaniem
                    </div>
                    <div class="fm-dup-live-actions">
                        <button class="fm-dup-btn" id="dup-rescan2"><i class="fas fa-redo"></i> Skanuj ponownie</button>
                    </div>
                `;
                header.querySelector('#dup-rescan2')?.addEventListener('click', () => _showConfig());
                statusbar.textContent = `Anulowano — ${liveGroups.length} grup znalezionych`;
            };

            const onError = (data) => {
                _cleanup();
                const header = content.querySelector('#dup-live-header');
                if (!header) return;
                header.innerHTML = `
                    <div class="fm-dup-results-info">
                        <i class="fas fa-exclamation-triangle" style="color:#ef4444"></i>
                        ${t('Błąd:')} ${data.error || t('Nieznany błąd')}
                    </div>
                    <div class="fm-dup-live-actions">
                        <button class="fm-dup-btn" id="dup-rescan2"><i class="fas fa-redo"></i> Skanuj ponownie</button>
                    </div>
                `;
                header.querySelector('#dup-rescan2')?.addEventListener('click', () => _showConfig());
            };

            NAS.socket?.on('dup_progress', onProgress);
            NAS.socket?.on('dup_complete', onComplete);
            NAS.socket?.on('dup_error', onError);
            NAS.socket?.on('dup_new_group', onNewGroup);
            NAS.socket?.on('dup_cancelled', onCancelled);

            // Delete key → trash checked
            const _onDupKeyDown = (e) => {
                if (e.target.tagName === 'INPUT' && e.target.type !== 'checkbox') return;
                if (e.key === 'Delete') {
                    e.preventDefault();
                    content.querySelector('#dup-trash-live')?.click();
                }
            };
            body.closest('.window')?.addEventListener('keydown', _onDupKeyDown);

            body._dupCleanup = () => {
                _cleanup();
                body.closest('.window')?.removeEventListener('keydown', _onDupKeyDown);
            };
        }

        // ─── Results view ───
        async function _showResults() {
            if (body._dupCleanup) { body._dupCleanup(); body._dupCleanup = null; }

            let data;
            try {
                data = await api('/files/duplicates/results');
            } catch {
                content.innerHTML = `<div class="fm-empty"><i class="fas fa-exclamation-triangle"></i><span>${t('Błąd ładowania wyników')}</span></div>`;
                return;
            }

            const groups = data.groups || [];
            if (!groups.length) {
                content.innerHTML = `
                    <div class="fm-empty">
                        <i class="fas fa-check-circle" style="color:var(--accent);opacity:1"></i>
                        <span>${t('Nie znaleziono duplikatów!')}</span>
                        <button class="fm-dup-scan-btn" id="dup-rescan" style="margin-top:12px"><i class="fas fa-redo"></i> Skanuj ponownie</button>
                    </div>`;
                content.querySelector('#dup-rescan')?.addEventListener('click', () => _showConfig());
                statusbar.textContent = t('Brak duplikatów');
                return;
            }

            const totalDups = groups.reduce((s, g) => s + g.items.length - 1, 0);
            const totalSize = groups.reduce((s, g) => s + g.items.slice(1).reduce((ss, f) => ss + f.size, 0), 0);

            content.innerHTML = `
                <div class="fm-dup-results-header">
                    <div class="fm-dup-results-info">
                        <i class="fas fa-clone"></i>
                        <strong>${groups.length}</strong> ${t('grup duplikatów &middot;')} <strong>${totalDups}</strong> ${t('nadmiarowych plików &middot;')} <strong>${formatBytes(totalSize)}</strong> ${t('do odzyskania')}
                    </div>
                    <div class="fm-dup-results-actions">
                        <button class="fm-dup-btn" id="dup-autoselect"><i class="fas fa-magic"></i> ${t('Auto-zaznacz duplikaty')}</button>
                        <button class="fm-dup-btn fm-dup-btn-danger" id="dup-trash-selected"><i class="fas fa-trash"></i> ${t('Do kosza zaznaczone')}</button>
                        <span class="fm-dup-actions-sep"></span>
                        <button class="fm-dup-btn" id="dup-select-all-groups"><i class="fas fa-check-double"></i> ${t('Zaznacz wszystkie grupy')}</button>
                        <button class="fm-dup-btn" id="dup-ignore-selected" style="display:none"><i class="fas fa-eye-slash"></i> ${t('Ignoruj zaznaczone')} (<span id="dup-ignore-count">0</span>)</button>
                        <span class="fm-dup-actions-sep"></span>
                        <button class="fm-dup-btn" id="dup-show-ignored"><i class="fas fa-eye-slash"></i> ${t('Ignorowane')}</button>
                        <button class="fm-dup-btn" id="dup-rescan2"><i class="fas fa-redo"></i> ${t('Skanuj ponownie')}</button>
                    </div>
                </div>
                <div class="fm-dup-groups" id="dup-groups">
                    ${groups.map((group, gi) => `
                        <div class="fm-dup-group" data-group="${gi}">
                            <div class="fm-dup-group-header">
                                <label class="fm-dup-group-cb"><input type="checkbox" class="fm-dup-group-select" data-group="${gi}"><span class="fm-cb-custom"></span></label>
                                <span class="fm-dup-group-badge ${group.type === 'exact' ? 'fm-dup-exact' : 'fm-dup-similar'}">
                                    ${group.type === 'exact' ? '<i class="fas fa-equals"></i> Identyczne' : '<i class="fas fa-eye"></i> Podobne'}
                                </span>
                                <span class="fm-dup-group-count">${group.items.length} ${t('plików')} &middot; ${formatBytes(group.items.reduce((s,f) => s + f.size, 0))}</span>
                                <button class="fm-dup-btn fm-dup-ignore-btn" data-group="${gi}" title="${t('Ignoruj tę grupę')}"><i class="fas fa-eye-slash"></i> ${t('Ignoruj')}</button>
                            </div>
                            <div class="fm-dup-group-items">
                                ${group.items.map((file, fi) => {
                                    const isFirst = fi === 0;
                                    const thumbSrc = `/api/files/preview?path=${encodeURIComponent(file.path)}&w=80&h=80`;
                                    return `
                                        <div class="fm-dup-file${isFirst ? ' fm-dup-original' : ''}" data-group="${gi}" data-idx="${fi}" data-path="${file.path}">
                                            <label class="fm-dup-file-cb">
                                                <input type="checkbox" data-group="${gi}" data-idx="${fi}">
                                                <span class="fm-cb-custom"></span>
                                            </label>
                                            <img src="${thumbSrc}" class="fm-dup-thumb" alt="" loading="lazy">
                                            <div class="fm-dup-file-info">
                                                <div class="fm-dup-file-name">${file.name}${isFirst ? ` <span class="fm-dup-keep-badge">${t('oryginał')}</span>` : ''}</div>
                                                <div class="fm-dup-file-meta">
                                                    <button class="fm-dup-open-fm" data-path="${file.path}" title="${t('Pokaż w Menedżerze plików')}"><i class="fas fa-folder-open"></i></button>
                                                    <span title="${file.path}">${file.path}</span>
                                                    <span>${formatBytes(file.size)}</span>
                                                    <span><i class="fas fa-calendar"></i> ${formatDate(file.modified)}</span>
                                                </div>
                                            </div>
                                        </div>
                                    `;
                                }).join('')}
                            </div>
                        </div>
                    `).join('')}
                </div>
            `;

            statusbar.textContent = t('Duplikaty:') + ' ' + groups.length + ' ' + t('grup,') + ' ' + totalDups + ' ' + t('nadmiarowych plików') + ' (' + formatBytes(totalSize) + ')';

            // Wire thumbnail clicks — navigate across ALL groups
            // Build flat list of all items for cross-group preview
            function _buildAllItems(groups) {
                const allItems = [];
                groups.forEach((g, gIdx) => {
                    g.items.forEach(f => allItems.push({ ...f, _groupIdx: gIdx, _groupType: g.type }));
                });
                return allItems;
            }
            content.querySelectorAll('.fm-dup-group').forEach(gEl => {
                // Wire "Show in FM" buttons
                gEl.querySelectorAll('.fm-dup-open-fm').forEach(btn => {
                    btn.addEventListener('click', (e) => {
                        e.stopPropagation();
                        const path = btn.dataset.path;
                        const folder = path.substring(0, path.lastIndexOf('/')) || '/';
                        const filename = path.split('/').pop();
                        openApp('file-manager', { path: folder, select: filename });
                    });
                });

                const gi = parseInt(gEl.dataset.group);
                const group = groups[gi];
                if (!group) return;
                gEl.querySelectorAll('.fm-dup-thumb').forEach(thumb => {
                    thumb.style.cursor = 'pointer';
                    thumb.addEventListener('click', (e) => {
                        e.stopPropagation();
                        const fileEl = thumb.closest('.fm-dup-file');
                        const fi = parseInt(fileEl?.dataset.idx || 0);
                        const allItems = _buildAllItems(groups);
                        // Compute global index
                        let globalIdx = 0;
                        for (let g = 0; g < gi; g++) globalIdx += groups[g].items.length;
                        globalIdx += fi;
                        _openDupPreview(allItems, globalIdx, (deleted) => {
                            const gIdx = deleted._groupIdx;
                            const groupDiv = content.querySelector(`.fm-dup-group[data-group="${gIdx}"]`);
                            if (groupDiv) {
                                const el = groupDiv.querySelector(`.fm-dup-file[data-path="${CSS.escape(deleted.path)}"]`);
                                if (el) el.remove();
                                if (groupDiv.querySelectorAll('.fm-dup-file').length < 2) groupDiv.remove();
                            }
                        });
                    });
                });
            });

            // Auto-select
            content.querySelector('#dup-autoselect')?.addEventListener('click', () => {
                content.querySelectorAll('.fm-dup-file-cb input[type="checkbox"]').forEach(cb => {
                    cb.checked = parseInt(cb.dataset.idx) !== 0;
                });
                _updateTrashBtn();
            });

            // Trash selected
            content.querySelector('#dup-trash-selected')?.addEventListener('click', async () => {
                const checked = [...content.querySelectorAll('.fm-dup-file-cb input:checked')];
                if (!checked.length) { toast(t('Zaznacz pliki do usunięcia'), 'warning'); return; }
                const paths = checked.map(cb => {
                    const gi = parseInt(cb.dataset.group);
                    const fi = parseInt(cb.dataset.idx);
                    return groups[gi].items[fi].path;
                });
                const sure = await confirmDialog(t('Do kosza'), t('Przenieść') + ' ' + paths.length + ' ' + t('duplikatów do kosza?'));
                if (!sure) return;
                try {
                    await api('/files/delete', { method: 'DELETE', body: { paths } });
                    toast(`Przeniesiono ${paths.length} ${t('duplikatów do kosza')}`, 'success');
                    await _showResults();
                } catch { toast(t('Błąd usuwania'), 'error'); }
            });

            // Rescan
            content.querySelector('#dup-rescan2')?.addEventListener('click', () => _showConfig());

            // Ignored tab
            content.querySelector('#dup-show-ignored')?.addEventListener('click', () => _showIgnored());

            // Ignore single group
            content.querySelectorAll('.fm-dup-ignore-btn').forEach(btn => {
                btn.addEventListener('click', async () => {
                    const gi = parseInt(btn.dataset.group);
                    const group = groups[gi];
                    if (!group) return;
                    try {
                        await api('/files/duplicates/ignore', { method: 'POST', body: { groups: [group] } });
                        toast(t('Grupa oznaczona jako ignorowana'), 'success');
                        btn.closest('.fm-dup-group')?.remove();
                    } catch { toast(t('Błąd ignorowania'), 'error'); }
                });
            });

            // Select all groups toggle
            let allGroupsSelected = false;
            content.querySelector('#dup-select-all-groups')?.addEventListener('click', () => {
                allGroupsSelected = !allGroupsSelected;
                content.querySelectorAll('.fm-dup-group-select').forEach(cb => { cb.checked = allGroupsSelected; });
                const btn = content.querySelector('#dup-select-all-groups');
                if (btn) btn.innerHTML = allGroupsSelected
                    ? '<i class="fas fa-times"></i> ' + t('Odznacz wszystkie grupy')
                    : '<i class="fas fa-check-double"></i> ' + t('Zaznacz wszystkie grupy');
                _updateGroupIgnoreBtn();
            });

            // Ignore selected groups
            content.querySelector('#dup-ignore-selected')?.addEventListener('click', async () => {
                const checkedGroups = [...content.querySelectorAll('.fm-dup-group-select:checked')];
                if (!checkedGroups.length) return;
                const groupsToIgnore = checkedGroups.map(cb => groups[parseInt(cb.dataset.group)]).filter(Boolean);
                const sure = await confirmDialog(t('Ignoruj grupy'), t('Oznaczyć') + ' ' + groupsToIgnore.length + ' ' + t('grup jako ignorowane?'));
                if (!sure) return;
                try {
                    await api('/files/duplicates/ignore', { method: 'POST', body: { groups: groupsToIgnore } });
                    toast(t('Zignorowano') + ' ' + groupsToIgnore.length + ' ' + t('grup'), 'success');
                    await _showResults();
                } catch { toast(t('Błąd ignorowania'), 'error'); }
            });

            // Group checkbox change
            content.querySelector('#dup-groups')?.addEventListener('change', (e) => {
                if (e.target.classList.contains('fm-dup-group-select')) _updateGroupIgnoreBtn();
                _updateTrashBtn();
            });

            function _updateGroupIgnoreBtn() {
                const count = content.querySelectorAll('.fm-dup-group-select:checked').length;
                const btn = content.querySelector('#dup-ignore-selected');
                const countEl = content.querySelector('#dup-ignore-count');
                if (btn) btn.style.display = count > 0 ? '' : 'none';
                if (countEl) countEl.textContent = count;
            }

            function _updateTrashBtn() {
                const count = content.querySelectorAll('.fm-dup-file-cb input:checked').length;
                const btn = content.querySelector('#dup-trash-selected');
                if (btn) btn.innerHTML = `<i class="fas fa-trash"></i> Do kosza (${count})`;
            }

            // Delete key
            const _onKey = (e) => {
                if (e.target.tagName === 'INPUT' && e.target.type !== 'checkbox') return;
                if (e.key === 'Delete') { e.preventDefault(); content.querySelector('#dup-trash-selected')?.click(); }
            };
            body.closest('.window')?.addEventListener('keydown', _onKey);
            body._dupCleanup = () => {
                body.closest('.window')?.removeEventListener('keydown', _onKey);
            };
        }

        // ─── Ignored groups view ───
        async function _showIgnored() {
            content.innerHTML = `<div class="fm-empty"><i class="fas fa-spinner fa-spin"></i><span>${t('Ładowanie…')}</span></div>`;

            let data;
            try {
                data = await api('/files/duplicates/ignored');
            } catch {
                content.innerHTML = `<div class="fm-empty"><i class="fas fa-exclamation-triangle"></i><span>${t('Błąd ładowania')}</span></div>`;
                return;
            }

            const items = data.items || [];
            if (!items.length) {
                content.innerHTML = `
                    <div class="fm-empty">
                        <i class="fas fa-eye-slash" style="opacity:.5"></i>
                        <span>${t('Brak ignorowanych grup')}</span>
                        <button class="fm-dup-btn" id="dup-back-results" style="margin-top:12px"><i class="fas fa-arrow-left"></i> ${t('Powrót do wyników')}</button>
                    </div>`;
                content.querySelector('#dup-back-results')?.addEventListener('click', () => _showResults());
                statusbar.textContent = t('Brak ignorowanych duplikatów');
                return;
            }

            content.innerHTML = `
                <div class="fm-dup-results-header">
                    <div class="fm-dup-results-info">
                        <i class="fas fa-eye-slash"></i>
                        <strong>${items.length}</strong> ${t('ignorowanych grup duplikatów')}
                    </div>
                    <div class="fm-dup-results-actions">
                        <button class="fm-dup-btn" id="dup-back-results"><i class="fas fa-arrow-left"></i> ${t('Powrót do wyników')}</button>
                        <button class="fm-dup-btn fm-dup-btn-danger" id="dup-unignore-all"><i class="fas fa-undo"></i> ${t('Przywróć wszystkie')}</button>
                    </div>
                </div>
                <div class="fm-dup-groups" id="dup-groups"></div>
            `;

            const groupsContainer = content.querySelector('#dup-groups');
            items.forEach((item, ii) => {
                const group = item.group;
                const paths = item.paths || [];
                const ignoredDate = item.ignored_at ? new Date(item.ignored_at * 1000).toLocaleDateString(getLocale()) : '';
                const div = document.createElement('div');
                div.className = 'fm-dup-group fm-dup-ignored-group';
                div.dataset.ignoreKey = item.key;
                div.dataset.idx = ii;

                if (group) {
                    let filesHtml = '';
                    group.items.forEach((file, fi) => {
                        const isFirst = fi === 0;
                        const thumbSrc = '/api/files/preview?path=' + encodeURIComponent(file.path) + '&w=80&h=80';
                        filesHtml += '<div class="fm-dup-file' + (isFirst ? ' fm-dup-original' : '') + '" data-path="' + file.path + '">'
                            + '<img src="' + thumbSrc + '" class="fm-dup-thumb" alt="" loading="lazy">'
                            + '<div class="fm-dup-file-info">'
                            + '<div class="fm-dup-file-name">' + file.name + (isFirst ? ` <span class="fm-dup-keep-badge">${t('oryginał')}</span>` : '') + '</div>'
                            + '<div class="fm-dup-file-meta">'
                            + '<span title="' + file.path + '"><i class="fas fa-folder-open"></i> ' + file.path + '</span>'
                            + '<span>' + formatBytes(file.size) + '</span>'
                            + '</div></div></div>';
                    });
                    div.innerHTML = '<div class="fm-dup-group-header">'
                        + '<span class="fm-dup-group-badge ' + (group.type === 'exact' ? 'fm-dup-exact' : 'fm-dup-similar') + '">'
                        + (group.type === 'exact' ? '<i class="fas fa-equals"></i> Identyczne' : '<i class="fas fa-eye"></i> Podobne')
                        + '</span>'
                        + '<span class="fm-dup-group-count">' + group.items.length + t(' plików · ') + formatBytes(group.items.reduce((s,f) => s + f.size, 0)) + '</span>'
                        + '<span class="fm-dup-ignored-date" title="Zignorowano"><i class="fas fa-calendar"></i> ' + ignoredDate + '</span>'
                        + '<button class="fm-dup-btn fm-dup-unignore-btn" data-key="' + item.key + `" title="${t('Przywróć')}"><i class="fas fa-undo"></i> ${t('Przywróć')}</button>`
                        + '</div>'
                        + '<div class="fm-dup-group-items">' + filesHtml + '</div>';
                } else {
                    div.innerHTML = '<div class="fm-dup-group-header">'
                        + '<span class="fm-dup-group-badge" style="opacity:.6"><i class="fas fa-eye-slash"></i> ' + (item.type || '?') + '</span>'
                        + '<span class="fm-dup-group-count">' + paths.length + ` ${t('plików')}</span>`
                        + '<span class="fm-dup-ignored-date"><i class="fas fa-calendar"></i> ' + ignoredDate + '</span>'
                        + '<button class="fm-dup-btn fm-dup-unignore-btn" data-key="' + item.key + `"><i class="fas fa-undo"></i> ${t('Przywróć')}</button>`
                        + '</div>'
                        + '<div class="fm-dup-group-items" style="padding:8px 14px;font-size:12px;color:var(--text-muted)">'
                        + paths.map(p => '<div><i class="fas fa-file-image" style="margin-right:6px"></i>' + p + '</div>').join('')
                        + '</div>';
                }
                groupsContainer.appendChild(div);

                if (group) {
                    div.querySelectorAll('.fm-dup-thumb').forEach(thumb => {
                        thumb.style.cursor = 'pointer';
                        thumb.addEventListener('click', (e) => {
                            e.stopPropagation();
                            const fileEl = thumb.closest('.fm-dup-file');
                            const fi = Array.from(div.querySelectorAll('.fm-dup-file')).indexOf(fileEl);
                            _openDupPreview(group.items, Math.max(fi, 0));
                        });
                    });
                }
            });

            statusbar.textContent = `Ignorowane duplikaty: ${items.length} grup`;

            content.querySelector('#dup-back-results')?.addEventListener('click', () => _showResults());

            content.querySelectorAll('.fm-dup-unignore-btn').forEach(btn => {
                btn.addEventListener('click', async () => {
                    const key = btn.dataset.key;
                    try {
                        await api('/files/duplicates/unignore', { method: 'POST', body: { keys: [key] } });
                        toast(t('Grupa przywrócona'), 'success');
                        btn.closest('.fm-dup-group')?.remove();
                    } catch { toast(t('Błąd'), 'error'); }
                });
            });

            content.querySelector('#dup-unignore-all')?.addEventListener('click', async () => {
                const keys = items.map(i => i.key);
                const sure = await confirmDialog(t('Przywróć wszystkie'), `${t('Przywrócić')} ${keys.length} ignorowanych grup?`);
                if (!sure) return;
                try {
                    await api('/files/duplicates/unignore', { method: 'POST', body: { keys } });
                    toast(`${t('Przywrócono')} ${keys.length} grup`, 'success');
                    _showIgnored();
                } catch { toast(t('Błąd'), 'error'); }
            });
        }
    }

    // Auto-maximize on small screens
    if (window.innerWidth <= 768) {
        toggleMaximize('duplicates');
    }
};
