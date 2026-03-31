/* ── EthOS Document Anonymizer ── */
/* globals AppRegistry, createWindow, NAS, api, t, showConfirm */

AppRegistry['doc-anonymizer'] = function (appDef) {
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

        // Check dependency status
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

        async function loadJobs() {
            var data = await api('/doc-anonymizer/jobs');
            if (!data || !data.items) { jobsDiv.innerHTML = ''; return; }

            if (data.items.length === 0) {
                jobsDiv.innerHTML = '<p class="anon-empty">' + t('Brak przetworzonych dokumentow') + '</p>';
                return;
            }

            var html = '<div class="anon-jobs-header">' + t('Historia') + '</div>';
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
                    showConfirm(
                        t('Usunac wynik anonimizacji?'),
                        async function () {
                            await api('/doc-anonymizer/job/' + id, { method: 'DELETE' });
                            loadJobs();
                        }
                    );
                });
            });

            // Click row to show details
            jobsDiv.querySelectorAll('.anon-job-row[data-job]').forEach(function (row) {
                row.addEventListener('click', function () {
                    showJobDetails(row.getAttribute('data-job'));
                });
            });
        }

        async function showJobDetails(jobId) {
            var data = await api('/doc-anonymizer/jobs');
            if (!data || !data.items) return;
            var job = data.items.find(function (j) { return j.job_id === jobId; });
            if (!job) return;

            var content = '<div class="anon-detail">';
            content += '<h3>' + (job.filename || '') + '</h3>';
            content += '<p>' + t('Status') + ': <b>' + (job.status || '') + '</b></p>';
            content += '<p>' + t('Stron') + ': ' + (job.pages_analyzed || 0) + '</p>';
            content += '<p>' + t('Znalezione PII') + ': ' + (job.entities_found || 0) + '</p>';

            if (job.replacements && job.replacements.length > 0) {
                content += '<table class="anon-detail-table"><thead><tr>' +
                    '<th>' + t('Kategoria') + '</th>' +
                    '<th>' + t('Oryginał') + '</th>' +
                    '<th>' + t('Zamiennik') + '</th>' +
                    '<th>' + t('Wystąpienia') + '</th>' +
                    '</tr></thead><tbody>';
                job.replacements.forEach(function (r) {
                    content += '<tr>' +
                        '<td><span class="anon-cat anon-cat-' + (r.category || '').toLowerCase() + '">' +
                            (r.category || '') + '</span></td>' +
                        '<td>' + (r.original || '') + '</td>' +
                        '<td><code>' + (r.placeholder || '') + '</code></td>' +
                        '<td>' + (r.occurrences || 0) + '</td>' +
                        '</tr>';
                });
                content += '</tbody></table>';
            }

            if (job.error) {
                content += '<p class="anon-error-msg">' + job.error + '</p>';
            }

            content += '</div>';

            createWindow('anon-detail-' + jobId, {
                title: t('Szczegoly anonimizacji'),
                icon: 'fa-shield-alt',
                iconColor: '#0ea5e9',
                width: 600, height: 450,
                onRender: function (b) { b.innerHTML = content; },
            });
        }
    }
};
