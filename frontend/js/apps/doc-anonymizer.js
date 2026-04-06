/* ── EthOS Document Anonymizer ── */
/* globals AppRegistry, createWindow, NAS, api, t, showConfirm */

AppRegistry['doc-anonymizer'] = function (appDef) {
    const _cl = (level, msg, details) => typeof NAS !== 'undefined' && NAS.logClient
        ? NAS.logClient('doc-anonymizer', level, msg, details) : console.log('[doc-anonymizer]', msg, details || '');

    createWindow('doc-anonymizer', {
        title: t('Document Anonymizer'),
        icon: appDef.icon,
        iconColor: appDef.color,
        width: 780, height: 600,
        onRender: function (body) { renderAnon(body); },
    });

    function renderAnon(body) {
        body.innerHTML =
            '<div class="anon-app">' +
                '<div class="anon-header">' +
                    '<i class="fa fa-user-shield"></i> ' +
                    '<span>' + t('Anonimizacja dokumentow medycznych') + '</span>' +
                '</div>' +
                '<div class="anon-body">' +
                    '<div class="anon-upload-zone" id="anon-drop">' +
                        '<i class="fa fa-cloud-upload-alt anon-upload-icon"></i>' +
                        '<p>' + t('Przeciagnij plik PDF lub DOCX') + '</p>' +
                        '<p class="anon-upload-hint">' + t('lub kliknij aby wybrac') + '</p>' +
                        '<input type="file" id="anon-file-input" accept=".pdf,.docx,.doc" style="display:none">' +
                    '</div>' +
                    '<div class="anon-status" id="anon-status" style="display:none">' +
                        '<div class="anon-progress-wrap">' +
                            '<svg class="anon-progress-ring" viewBox="0 0 60 60">' +
                                '<circle class="anon-ring-bg" cx="30" cy="30" r="26"></circle>' +
                                '<circle class="anon-ring-fg" id="anon-ring" cx="30" cy="30" r="26"></circle>' +
                            '</svg>' +
                            '<span class="anon-progress-pct" id="anon-pct">0%</span>' +
                        '</div>' +
                        '<p class="anon-status-msg" id="anon-msg"></p>' +
                    '</div>' +
                    '<div class="anon-jobs" id="anon-jobs"></div>' +
                '</div>' +
                '<div class="anon-dep-warning" id="anon-dep-warn" style="display:none">' +
                    '<i class="fa fa-exclamation-triangle"></i> ' +
                    '<span id="anon-dep-msg"></span>' +
                '</div>' +
            '</div>';

        var dropZone = body.querySelector('#anon-drop');
        var fileInput = body.querySelector('#anon-file-input');
        var statusDiv = body.querySelector('#anon-status');
        var jobsDiv = body.querySelector('#anon-jobs');
        var depWarn = body.querySelector('#anon-dep-warn');
        var ringEl = body.querySelector('#anon-ring');
        var pctEl = body.querySelector('#anon-pct');
        var msgEl = body.querySelector('#anon-msg');
        var circumference = 2 * Math.PI * 26;

        if (ringEl) {
            ringEl.style.strokeDasharray = circumference;
            ringEl.style.strokeDashoffset = circumference;
        }

        checkDeps();
        loadJobs();

        // Socket listener for progress
        if (NAS.socket) {
            NAS.socket.off('anon_progress');
            NAS.socket.on('anon_progress', function (d) {
                if (d.stage === 'done' || d.stage === 'error') {
                    setTimeout(function () {
                        statusDiv.style.display = 'none';
                        dropZone.style.display = '';
                        loadJobs();
                    }, 1500);
                }
                setProgress(d.percent || 0, d.message || '');
            });
        }

        // Drag and drop
        dropZone.addEventListener('dragover', function (e) {
            e.preventDefault();
            dropZone.classList.add('anon-drag-over');
        });
        dropZone.addEventListener('dragleave', function () {
            dropZone.classList.remove('anon-drag-over');
        });
        dropZone.addEventListener('drop', function (e) {
            e.preventDefault();
            dropZone.classList.remove('anon-drag-over');
            if (e.dataTransfer.files.length) uploadFile(e.dataTransfer.files[0]);
        });
        dropZone.addEventListener('click', function () { fileInput.click(); });
        fileInput.addEventListener('change', function () {
            if (fileInput.files.length) uploadFile(fileInput.files[0]);
        });

        function resetProgress() {
            pctEl.textContent = '0%';
            msgEl.textContent = '';
            if (ringEl) ringEl.style.strokeDashoffset = circumference;
        }

        function setProgress(pct, msg) {
            statusDiv.style.display = '';
            pctEl.textContent = Math.round(pct) + '%';
            msgEl.textContent = msg || '';
            if (ringEl) {
                var offset = circumference - (pct / 100) * circumference;
                ringEl.style.strokeDashoffset = offset;
            }
        }

        async function checkDeps() {
            var data = await api('/doc-anonymizer/status');
            if (!data || data.error) return;
            if (!data.ai_chat_available) {
                depWarn.style.display = '';
                body.querySelector('#anon-dep-msg').textContent =
                    t('AI Assistant nie jest zainstalowany. Zainstaluj go w Package Center.');
                dropZone.style.opacity = '0.4';
                dropZone.style.pointerEvents = 'none';
            } else if (!data.active_model) {
                depWarn.style.display = '';
                body.querySelector('#anon-dep-msg').textContent =
                    t('Brak aktywnego modelu LLM. Otworz AI Assistant i pobierz model Bielik 7B.');
                dropZone.style.opacity = '0.4';
                dropZone.style.pointerEvents = 'none';
            } else {
                depWarn.style.display = 'none';
                dropZone.style.opacity = '';
                dropZone.style.pointerEvents = '';
            }
        }

        async function uploadFile(file) {
            var ext = file.name.split('.').pop().toLowerCase();
            if (['pdf', 'docx', 'doc'].indexOf(ext) === -1) {
                if (typeof showToast === 'function') showToast(t('Obslugiwane formaty: PDF, DOCX'), 'error');
                return;
            }

            dropZone.style.display = 'none';
            resetProgress();
            setProgress(5, t('Wysylanie pliku...'));

            var fd = new FormData();
            fd.append('file', file);

            try {
                var resp = await fetch('/api/doc-anonymizer/upload', {
                    method: 'POST',
                    headers: {
                        'Authorization': 'Bearer ' + NAS.token,
                        'X-CSRF-Token': NAS.token,
                    },
                    body: fd,
                });
                var data = await resp.json();
                if (data.error) {
                    setProgress(0, data.error);
                    setTimeout(function () {
                        statusDiv.style.display = 'none';
                        dropZone.style.display = '';
                    }, 3000);
                    return;
                }
                setProgress(10, t('Anonimizacja w toku...'));
            } catch (e) {
                setProgress(0, 'Upload failed: ' + e.message);
                setTimeout(function () {
                    statusDiv.style.display = 'none';
                    dropZone.style.display = '';
                }, 3000);
            }
        }

        /* ── Multi-select helpers ── */

        function getSelectedIds() {
            var checked = jobsDiv.querySelectorAll('.anon-job-cb:checked');
            return Array.from(checked).map(function (cb) { return cb.value; });
        }

        function updateBatchBar() {
            var bar = jobsDiv.querySelector('.anon-batch-bar');
            var ids = getSelectedIds();
            if (bar) bar.style.display = ids.length ? '' : 'none';
            var countEl = bar && bar.querySelector('.anon-batch-count');
            if (countEl) countEl.textContent = ids.length;
        }

        async function deleteBatch() {
            var ids = getSelectedIds();
            if (!ids.length) return;
            confirmDialog(
                t('Usunac zaznaczone dokumenty?') + ' (' + ids.length + ')',
                async function () {
                    await api('/doc-anonymizer/jobs/delete-batch', {
                        method: 'POST',
                        headers: { 'Content-Type': 'application/json' },
                        body: JSON.stringify({ job_ids: ids }),
                    });
                    loadJobs();
                }
            );
        }

        /* ── Jobs list ── */

        async function loadJobs() {
            var data = await api('/doc-anonymizer/jobs');
            if (!data || !data.items) { jobsDiv.innerHTML = ''; return; }

            if (data.items.length === 0) {
                jobsDiv.innerHTML = '<p class="anon-empty">' + t('Brak przetworzonych dokumentow') + '</p>';
                return;
            }

            var html = '<div class="anon-jobs-toolbar">' +
                '<div class="anon-jobs-header">' + t('Historia') + '</div>' +
                '<div class="anon-batch-bar" style="display:none">' +
                    '<span class="anon-batch-count">0</span> ' + t('zaznaczonych') +
                    '<button class="anon-btn anon-btn-batch-del" id="anon-batch-del">' +
                        '<i class="fa fa-trash"></i> ' + t('Usun zaznaczone') +
                    '</button>' +
                '</div>' +
            '</div>';

            data.items.forEach(function (job) {
                var dateStr = job.created_at ? new Date(job.created_at * 1000).toLocaleString() : '';
                var statusIcon = '';
                var statusClass = '';
                if (job.status === 'done') {
                    statusIcon = '<i class="fa fa-check-circle"></i>';
                    statusClass = 'anon-job-done';
                } else if (job.status === 'error') {
                    statusIcon = '<i class="fa fa-times-circle"></i>';
                    statusClass = 'anon-job-error';
                } else {
                    statusIcon = '<i class="fa fa-spinner fa-spin"></i>';
                    statusClass = 'anon-job-processing';
                }

                html += '<div class="anon-job-row ' + statusClass + '" data-job="' + job.job_id + '">' +
                    '<label class="anon-job-cb-label" onclick="event.stopPropagation()">' +
                        '<input type="checkbox" class="anon-job-cb" value="' + job.job_id + '">' +
                    '</label>' +
                    '<div class="anon-job-info">' +
                        statusIcon + ' ' +
                        '<span class="anon-job-name">' + (job.filename || '') + '</span>' +
                        '<span class="anon-job-date">' + dateStr + '</span>' +
                    '</div>' +
                    '<div class="anon-job-actions">';

                if (job.status === 'done') {
                    html += '<span class="anon-job-entities">' +
                        (job.entities_found || 0) + ' ' + t('PII') +
                        '</span>' +
                        '<button class="anon-btn anon-btn-dl" data-dl="' + job.job_id + '" title="' + t('Pobierz') + '">' +
                            '<i class="fa fa-download"></i>' +
                        '</button>';
                }

                html += '<button class="anon-btn anon-btn-del" data-del="' + job.job_id + '" title="' + t('Usun') + '">' +
                        '<i class="fa fa-trash"></i>' +
                    '</button>' +
                    '</div></div>';
            });

            jobsDiv.innerHTML = html;

            // Checkbox change → update batch bar
            jobsDiv.querySelectorAll('.anon-job-cb').forEach(function (cb) {
                cb.addEventListener('change', updateBatchBar);
            });

            // Batch delete button
            var batchBtn = jobsDiv.querySelector('#anon-batch-del');
            if (batchBtn) batchBtn.addEventListener('click', deleteBatch);

            // Download buttons
            jobsDiv.querySelectorAll('[data-dl]').forEach(function (btn) {
                btn.addEventListener('click', function (e) {
                    e.stopPropagation();
                    var id = btn.getAttribute('data-dl');
                    window.open('/api/doc-anonymizer/download/' + id + '?token=' + NAS.token, '_blank');
                });
            });

            // Delete buttons
            jobsDiv.querySelectorAll('[data-del]').forEach(function (btn) {
                btn.addEventListener('click', function (e) {
                    e.stopPropagation();
                    var id = btn.getAttribute('data-del');
                    confirmDialog(
                        t('Usunac wynik anonimizacji?'),
                        async function () {
                            await api('/doc-anonymizer/job/' + id, { method: 'DELETE' });
                            loadJobs();
                        }
                    );
                });
            });

            // Click row to show preview
            jobsDiv.querySelectorAll('.anon-job-row[data-job]').forEach(function (row) {
                row.addEventListener('click', function () {
                    showJobPreview(row.getAttribute('data-job'));
                });
            });
        }

        /* ── Split-pane preview window ── */

        async function showJobPreview(jobId) {
            var data = await api('/doc-anonymizer/jobs');
            if (!data || !data.items) return;
            var job = data.items.find(function (j) { return j.job_id === jobId; });
            if (!job) return;

            var isPdf = (job.file_ext === '.pdf');
            var isDone = (job.status === 'done');

            createWindow('anon-preview-' + jobId, {
                title: (job.filename || '') + ' — ' + t('Porownanie'),
                icon: 'fa-columns',
                iconColor: '#0ea5e9',
                width: 1100, height: 700,
                onRender: function (b) { renderPreview(b, job, isPdf, isDone); },
            });
        }

        function renderPreview(b, job, isPdf, isDone) {
            var jobId = job.job_id;

            // Build replacement table HTML with toggle switches
            var tableHtml = '';
            if (job.replacements && job.replacements.length > 0) {
                tableHtml = '<table class="anon-detail-table"><thead><tr>' +
                    '<th>' + t('Anonimizuj') + '</th>' +
                    '<th>' + t('Kategoria') + '</th>' +
                    '<th>' + t('Oryginal') + '</th>' +
                    '<th>' + t('Zamiennik') + '</th>' +
                    '<th>' + t('Wystapienia') + '</th>' +
                    '</tr></thead><tbody>';
                job.replacements.forEach(function (r, idx) {
                    var rid = 'anon-toggle-' + jobId + '-' + idx;
                    tableHtml += '<tr data-original="' + (r.original || '').replace(/"/g, '&quot;') + '">' +
                        '<td><label class="anon-toggle"><input type="checkbox" checked id="' + rid + '" ' +
                            'data-original="' + (r.original || '').replace(/"/g, '&quot;') + '">' +
                            '<span class="anon-toggle-slider"></span></label></td>' +
                        '<td><span class="anon-cat anon-cat-' + (r.category || '').toLowerCase() + '">' +
                            (r.category || '') + '</span></td>' +
                        '<td>' + (r.original || '') + '</td>' +
                        '<td><code>' + (r.placeholder || '') + '</code></td>' +
                        '<td>' + (r.occurrences || 0) + '</td>' +
                        '</tr>';
                });
                tableHtml += '</tbody></table>' +
                    '<div class="anon-regen-bar">' +
                        '<span class="anon-regen-hint"><i class="fa fa-info-circle"></i> ' +
                            t('Odznacz elementy ktore nie powinny byc anonimizowane (false positives)') + '</span>' +
                        '<button class="anon-regen-btn" id="anon-regen-' + jobId + '" disabled>' +
                            '<i class="fa fa-sync-alt"></i> ' + t('Regeneruj PDF') +
                        '</button>' +
                    '</div>';
            }

            // Meta + zoom toolbar
            var metaHtml = '<div class="anon-preview-meta">' +
                '<span><i class="fa fa-file"></i> ' + (job.filename || '') + '</span>' +
                '<span><i class="fa fa-copy"></i> ' + (job.pages_analyzed || 0) + ' ' + t('stron') + '</span>' +
                '<span><i class="fa fa-shield-alt"></i> ' + (job.entities_found || 0) + ' PII</span>' +
                '<span class="anon-zoom-bar">' +
                    '<button class="anon-zoom-btn" data-action="out" title="Zoom -"><i class="fa fa-search-minus"></i></button>' +
                    '<span class="anon-zoom-level" id="anon-zoom-lbl-' + jobId + '">100%</span>' +
                    '<button class="anon-zoom-btn" data-action="in" title="Zoom +"><i class="fa fa-search-plus"></i></button>' +
                    '<button class="anon-zoom-btn" data-action="reset" title="Reset"><i class="fa fa-undo"></i></button>' +
                '</span>' +
                '</div>';

            var content = '<div class="anon-preview">' + metaHtml;

            if (!isDone) {
                content += '<div class="anon-preview-pending">' +
                    '<i class="fa fa-spinner fa-spin"></i> ' +
                    t('Anonimizacja jeszcze nie zakonczona') +
                    '</div>';
                if (job.error) {
                    content += '<p class="anon-error-msg">' + job.error + '</p>';
                }
                content += '</div>';
                b.innerHTML = content;
                return;
            }

            // Split pane
            content += '<div class="anon-preview-split">' +
                '<div class="anon-preview-pane">' +
                    '<div class="anon-preview-pane-title">' +
                        '<i class="fa fa-file-alt"></i> ' + t('Oryginal') +
                    '</div>' +
                    '<div class="anon-preview-content" id="anon-pv-orig-' + jobId + '">' +
                        '<div class="anon-preview-loading"><i class="fa fa-spinner fa-spin"></i></div>' +
                    '</div>' +
                '</div>' +
                '<div class="anon-preview-divider"></div>' +
                '<div class="anon-preview-pane">' +
                    '<div class="anon-preview-pane-title anon-preview-pane-title-anon">' +
                        '<i class="fa fa-user-shield"></i> ' + t('Zanonimizowany') +
                    '</div>' +
                    '<div class="anon-preview-content" id="anon-pv-anon-' + jobId + '">' +
                        '<div class="anon-preview-loading"><i class="fa fa-spinner fa-spin"></i></div>' +
                    '</div>' +
                '</div>' +
            '</div>';

            if (tableHtml) {
                content += '<div class="anon-preview-table-wrap">' +
                    '<div class="anon-preview-table-title">' +
                        '<i class="fa fa-exchange-alt"></i> ' + t('Zamienniki') +
                        ' <span class="anon-preview-table-count">(' + (job.replacements || []).length + ')</span>' +
                    '</div>' +
                    tableHtml +
                '</div>';
            }

            content += '</div>';
            b.innerHTML = content;

            var origEl = b.querySelector('#anon-pv-orig-' + jobId);
            var anonEl = b.querySelector('#anon-pv-anon-' + jobId);

            // Load content into both panes
            if (isPdf) {
                loadPdfPreview(jobId, 'original', origEl);
                loadPdfPreview(jobId, 'anonymized', anonEl);
            } else {
                loadDocxPreview(jobId, 'original', origEl);
                loadDocxPreview(jobId, 'anonymized', anonEl);
            }

            // Synchronized scrolling
            var syncing = false;
            origEl.addEventListener('scroll', function () {
                if (syncing) return;
                syncing = true;
                anonEl.scrollTop = origEl.scrollTop;
                anonEl.scrollLeft = origEl.scrollLeft;
                syncing = false;
            });
            anonEl.addEventListener('scroll', function () {
                if (syncing) return;
                syncing = true;
                origEl.scrollTop = anonEl.scrollTop;
                origEl.scrollLeft = anonEl.scrollLeft;
                syncing = false;
            });

            // Zoom controls — re-render PDF pages at new scale
            var currentZoom = 1.0;
            var zoomLabel = b.querySelector('#anon-zoom-lbl-' + jobId);

            function applyZoom(level) {
                currentZoom = Math.max(0.25, Math.min(3.0, level));
                if (zoomLabel) zoomLabel.textContent = Math.round(currentZoom * 100) + '%';
                if (isPdf) {
                    renderAllPdfCanvases(origEl, currentZoom);
                    renderAllPdfCanvases(anonEl, currentZoom);
                } else {
                    [origEl, anonEl].forEach(function (el) {
                        var txt = el.querySelector('.anon-preview-text');
                        if (txt) {
                            txt.style.transform = 'scale(' + currentZoom + ')';
                            txt.style.transformOrigin = 'top left';
                            txt.style.width = (100 / currentZoom) + '%';
                        }
                    });
                }
            }

            b.querySelectorAll('.anon-zoom-btn').forEach(function (btn) {
                btn.addEventListener('click', function () {
                    var action = btn.dataset.action;
                    if (action === 'in') applyZoom(currentZoom + 0.15);
                    else if (action === 'out') applyZoom(currentZoom - 0.15);
                    else applyZoom(1.0);
                });
            });

            var splitEl = b.querySelector('.anon-preview-split');
            if (splitEl) {
                splitEl.addEventListener('wheel', function (e) {
                    if (e.ctrlKey || e.metaKey) {
                        e.preventDefault();
                        applyZoom(currentZoom + (e.deltaY < 0 ? 0.1 : -0.1));
                    }
                }, { passive: false });
            }

            // Toggle switches + regenerate button
            var regenBtn = b.querySelector('#anon-regen-' + jobId);
            var toggles = b.querySelectorAll('.anon-toggle input[type="checkbox"]');
            if (regenBtn && toggles.length) {
                toggles.forEach(function (cb) {
                    cb.addEventListener('change', function () {
                        var row = cb.closest('tr');
                        if (row) row.classList.toggle('anon-row-excluded', !cb.checked);
                        var anyUnchecked = Array.from(toggles).some(function (c) { return !c.checked; });
                        regenBtn.disabled = !anyUnchecked;
                    });
                });

                regenBtn.addEventListener('click', async function () {
                    var excluded = [];
                    toggles.forEach(function (cb) {
                        if (!cb.checked) excluded.push(cb.dataset.original);
                    });
                    if (!excluded.length) return;

                    regenBtn.disabled = true;
                    regenBtn.innerHTML = '<i class="fa fa-spinner fa-spin"></i> ' + t('Regenerowanie...');

                    try {
                        var res = await api('/doc-anonymizer/regenerate/' + jobId, {
                            method: 'POST',
                            body: { excluded: excluded },
                        });
                        if (res.error) {
                            toast(res.error, 'error');
                            regenBtn.disabled = false;
                            regenBtn.innerHTML = '<i class="fa fa-sync-alt"></i> ' + t('Regeneruj PDF');
                            return;
                        }
                        toast(t('PDF zregenerowany') + ' — ' + res.excluded_count + ' ' + t('elementow przywroconych'), 'success');
                        // Reload the anonymized pane
                        var anonPane = b.querySelector('#anon-pv-anon-' + jobId);
                        if (anonPane) {
                            anonPane.innerHTML = '<div class="anon-preview-loading"><i class="fa fa-spinner fa-spin"></i></div>';
                            if (isPdf) loadPdfPreview(jobId, 'anonymized', anonPane);
                            else loadDocxPreview(jobId, 'anonymized', anonPane);
                        }
                        // Update replacement table with new data
                        if (res.replacements) {
                            job.replacements = res.replacements;
                            job.entities_found = res.entities_found;
                        }
                        regenBtn.innerHTML = '<i class="fa fa-check"></i> ' + t('Gotowe');
                        setTimeout(function () {
                            regenBtn.innerHTML = '<i class="fa fa-sync-alt"></i> ' + t('Regeneruj PDF');
                        }, 2000);
                    } catch (e) {
                        toast(t('Blad regeneracji') + ': ' + e.message, 'error');
                        regenBtn.disabled = false;
                        regenBtn.innerHTML = '<i class="fa fa-sync-alt"></i> ' + t('Regeneruj PDF');
                    }
                });
            }
        }

        /* Re-render all PDF canvases in a container at the given zoom */
        function renderAllPdfCanvases(container, zoom) {
            container.querySelectorAll('canvas[data-page-idx]').forEach(function (canvas) {
                var page = canvas._pdfPage;
                if (!page) return;
                var vp = page.getViewport({ scale: zoom * 1.5 });
                canvas.width = vp.width;
                canvas.height = vp.height;
                canvas.style.width = vp.width / 1.5 + 'px';
                canvas.style.height = vp.height / 1.5 + 'px';
                page.render({ canvasContext: canvas.getContext('2d'), viewport: vp });
            });
        }

        /* Load and render a PDF using PDF.js */
        async function loadPdfPreview(jobId, which, container) {
            try {
                // Lazy-load PDF.js
                if (!window.pdfjsLib) {
                    await new Promise(function (resolve, reject) {
                        var s = document.createElement('script');
                        s.src = '/lib/pdf.min.js';
                        s.onload = resolve;
                        s.onerror = reject;
                        document.head.appendChild(s);
                    });
                    pdfjsLib.GlobalWorkerOptions.workerSrc = '/lib/pdf.worker.min.js';
                }

                var url = '/api/doc-anonymizer/preview/' + jobId + '/' + which +
                    '?token=' + encodeURIComponent(NAS.token);
                var pdf = await pdfjsLib.getDocument(url).promise;

                container.innerHTML = '';
                var wrapper = document.createElement('div');
                wrapper.className = 'anon-pdf-pages';
                container.appendChild(wrapper);

                for (var i = 1; i <= pdf.numPages; i++) {
                    var page = await pdf.getPage(i);
                    var vp = page.getViewport({ scale: 1.5 });
                    var canvas = document.createElement('canvas');
                    canvas.className = 'anon-pdf-canvas';
                    canvas.dataset.pageIdx = i;
                    canvas.width = vp.width;
                    canvas.height = vp.height;
                    canvas.style.width = vp.width / 1.5 + 'px';
                    canvas.style.height = vp.height / 1.5 + 'px';
                    canvas._pdfPage = page;
                    wrapper.appendChild(canvas);
                    page.render({ canvasContext: canvas.getContext('2d'), viewport: vp });
                }
            } catch (e) {
                container.innerHTML = '<div class="anon-preview-error">' +
                    t('Nie udalo sie zaladowac PDF') + ': ' + e.message + '</div>';
            }
        }

        async function loadDocxPreview(jobId, which, container) {
            try {
                var data = await api('/doc-anonymizer/preview/' + jobId + '/' + which);
                if (data && data.text) {
                    var escaped = data.text
                        .replace(/&/g, '&amp;')
                        .replace(/</g, '&lt;')
                        .replace(/>/g, '&gt;')
                        .replace(/\n/g, '<br>');
                    container.innerHTML = '<div class="anon-preview-text">' + escaped + '</div>';
                } else {
                    container.innerHTML = '<div class="anon-preview-error">' +
                        (data && data.error ? data.error : t('Nie udalo sie zaladowac podgladu')) +
                        '</div>';
                }
            } catch (e) {
                container.innerHTML = '<div class="anon-preview-error">' + e.message + '</div>';
            }
        }
    }
};
