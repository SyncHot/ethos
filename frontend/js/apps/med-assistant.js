/* == EthOS Medical Assistant == */
/* globals AppRegistry, createWindow, NAS, api, t, confirmDialog */

function _medMd(text) {
    var s = text.replace(/&/g, '&amp;').replace(/</g, '&lt;').replace(/>/g, '&gt;');
    // tables
    s = s.replace(/((?:^\|.+\|$\n?)+)/gm, function (block) {
        var rows = block.trim().split('\n').filter(function (r) { return r.trim(); });
        if (rows.length < 2) return block;
        var html = '<table class="med-md-table">';
        rows.forEach(function (row, i) {
            if (/^\|[\s:-]+\|$/.test(row.replace(/[:-]/g, function (c) { return c; }))) return; // skip separator
            if (/^\|\s*[-:]+/.test(row) && !/[a-zA-Z0-9]/.test(row.replace(/[|:\-\s]/g, ''))) return;
            var cells = row.split('|').filter(function (c, j, a) { return j > 0 && j < a.length - 1; });
            var tag = i === 0 ? 'th' : 'td';
            html += '<tr>' + cells.map(function (c) {
                return '<' + tag + '>' + c.trim() + '</' + tag + '>';
            }).join('') + '</tr>';
        });
        html += '</table>';
        return html;
    });
    // headers
    s = s.replace(/^### (.+)$/gm, '<h4 class="med-md-h">$1</h4>');
    s = s.replace(/^## (.+)$/gm, '<h3 class="med-md-h">$1</h3>');
    s = s.replace(/^# (.+)$/gm, '<h2 class="med-md-h">$1</h2>');
    // horizontal rule
    s = s.replace(/^---+$/gm, '<hr class="med-md-hr">');
    // bold + italic
    s = s.replace(/\*\*(.+?)\*\*/g, '<strong>$1</strong>');
    s = s.replace(/\*(.+?)\*/g, '<em>$1</em>');
    // inline code
    s = s.replace(/`([^`]+)`/g, '<code class="med-md-code">$1</code>');
    // unordered lists
    s = s.replace(/^- (.+)$/gm, '<li>$1</li>');
    s = s.replace(/((?:<li>.+<\/li>\n?)+)/g, '<ul class="med-md-list">$1</ul>');
    // alert emoji
    s = s.replace(/⚠️/g, '<span class="med-alert-icon">⚠️</span>');
    // newlines (preserve spacing outside tables/lists)
    s = s.replace(/\n/g, '<br>');
    // clean up extra <br> around block elements
    s = s.replace(/<br>(<h[234]|<table|<ul|<hr)/g, '$1');
    s = s.replace(/(<\/h[234]>|<\/table>|<\/ul>|<hr[^>]*>)<br>/g, '$1');
    return s;
}

AppRegistry['med-assistant'] = function (appDef) {
    createWindow('med-assistant', {
        title: t('Medical Assistant'),
        icon: appDef.icon,
        iconColor: appDef.color,
        width: 960, height: 680,
        onRender: function (body) { renderMedApp(body); },
    });

    function renderMedApp(body) {
        body.innerHTML =
            '<div class="med-app">' +
                '<div class="med-header">' +
                    '<i class="fa fa-user-md"></i> ' +
                    '<span>' + t('Medical Assistant') + '</span>' +
                    '<span class="med-disclaimer">' + t('Narzedzie wspomagajace -- nie zastepuje oceny lekarza') + '</span>' +
                '</div>' +
                '<div class="med-dep-warning" id="med-dep-warn" style="display:none">' +
                    '<i class="fa fa-exclamation-triangle"></i> ' +
                    '<span id="med-dep-msg"></span>' +
                '</div>' +
                '<div class="med-body">' +
                    '<div class="med-sidebar" id="med-sidebar"></div>' +
                    '<div class="med-content" id="med-content">' +
                        '<div class="med-placeholder">' +
                            '<i class="fa fa-folder-open"></i>' +
                            '<p>' + t('Wybierz lub utworz folder pacjenta') + '</p>' +
                        '</div>' +
                    '</div>' +
                '</div>' +
            '</div>';

        var sidebar = body.querySelector('#med-sidebar');
        var content = body.querySelector('#med-content');
        var depWarn = body.querySelector('#med-dep-warn');
        var currentPatient = null;
        var selectedFiles = [];

        checkDeps();
        loadPatients();

        if (NAS.socket) {
            NAS.socket.off('med_progress');
            NAS.socket.on('med_progress', function (d) {
                var pctEl = content.querySelector('#med-job-pct');
                var msgEl = content.querySelector('#med-job-msg');
                var ringEl = content.querySelector('#med-ring');
                if (pctEl) pctEl.textContent = Math.round(d.percent || 0) + '%';
                if (msgEl) msgEl.textContent = d.message || '';
                if (ringEl) {
                    var circ = 2 * Math.PI * 26;
                    ringEl.style.strokeDashoffset = circ - ((d.percent || 0) / 100) * circ;
                }
                if (d.stage === 'done' || d.stage === 'error') {
                    setTimeout(function () {
                        if (currentPatient) showPatient(currentPatient);
                    }, 1500);
                }
            });
        }

        async function checkDeps() {
            var data = await api('/med-assistant/status');
            if (!data || data.error) return;
            if (!data.ai_chat_available) {
                depWarn.style.display = '';
                body.querySelector('#med-dep-msg').textContent =
                    t('AI Assistant nie jest zainstalowany. Zainstaluj go w Package Center.');
            } else if (!data.active_model) {
                depWarn.style.display = '';
                body.querySelector('#med-dep-msg').textContent =
                    t('Brak aktywnego modelu LLM. Otworz AI Assistant i pobierz model Bielik.');
            }
        }

        async function loadPatients() {
            var data = await api('/med-assistant/patients');
            if (!data || !data.items) return;

            var html = '<div class="med-sidebar-header">' +
                '<span>' + t('Pacjenci') + '</span>' +
                '<button class="med-btn-icon" id="med-add-patient" title="' + t('Nowy pacjent') + '">' +
                    '<i class="fa fa-plus"></i>' +
                '</button>' +
            '</div>';

            if (data.items.length === 0) {
                html += '<p class="med-empty-hint">' + t('Brak folderow pacjentow') + '</p>';
            }

            data.items.forEach(function (p) {
                var active = currentPatient === p.name ? ' med-patient-active' : '';
                html += '<div class="med-patient-row' + active + '" data-patient="' + p.name + '">' +
                    '<i class="fa fa-folder"></i> ' +
                    '<span class="med-patient-name">' + p.name + '</span>' +
                    '<span class="med-patient-count">' + p.file_count + '</span>' +
                '</div>';
            });

            sidebar.innerHTML = html;

            sidebar.querySelector('#med-add-patient').addEventListener('click', addPatient);

            sidebar.querySelectorAll('.med-patient-row').forEach(function (row) {
                row.addEventListener('click', function () {
                    currentPatient = row.getAttribute('data-patient');
                    loadPatients();
                    showPatient(currentPatient);
                });
            });
        }

        function addPatient() {
            var name = prompt(t('Nazwa folderu pacjenta:'));
            if (!name || !name.trim()) return;
            api('/med-assistant/patients', {
                method: 'POST',
                body: { name: name.trim() },
            }).then(function (data) {
                if (data && data.ok) {
                    currentPatient = data.name;
                    loadPatients();
                    showPatient(data.name);
                } else if (data && data.error) {
                    if (typeof showToast === 'function') showToast(data.error, 'error');
                }
            });
        }

        async function showPatient(name) {
            selectedFiles = [];
            var data = await api('/med-assistant/patients/' + encodeURIComponent(name) + '/files');
            if (!data || data.error) return;

            var html = '<div class="med-patient-header">' +
                '<h3><i class="fa fa-folder-open"></i> ' + name + '</h3>' +
                '<div class="med-patient-actions">' +
                    '<label class="med-btn med-btn-sm med-btn-upload">' +
                        '<i class="fa fa-upload"></i> ' + t('Dodaj pliki') +
                        '<input type="file" id="med-file-input" multiple accept=".pdf,.docx,.doc,.txt" style="display:none">' +
                    '</label>' +
                    '<button class="med-btn med-btn-sm med-btn-danger" id="med-del-patient">' +
                        '<i class="fa fa-trash"></i> ' + t('Usun folder') +
                    '</button>' +
                '</div>' +
            '</div>';

            // File list
            if (data.items.length === 0) {
                html += '<div class="med-empty-files">' +
                    '<i class="fa fa-file-medical"></i>' +
                    '<p>' + t('Dodaj dokumenty medyczne (PDF, DOCX, TXT)') + '</p>' +
                '</div>';
            } else {
                html += '<div class="med-files-header">' +
                    '<label class="med-select-all"><input type="checkbox" id="med-select-all"> ' + t('Zaznacz wszystkie') + '</label>' +
                '</div>';
                html += '<div class="med-files-list">';
                data.items.forEach(function (f) {
                    var icon = f.type === '.pdf' ? 'fa-file-pdf' :
                               f.type === '.docx' || f.type === '.doc' ? 'fa-file-word' : 'fa-file-alt';
                    var sizeStr = f.size < 1024 ? f.size + ' B' :
                                  f.size < 1048576 ? (f.size / 1024).toFixed(1) + ' KB' :
                                  (f.size / 1048576).toFixed(1) + ' MB';
                    html += '<div class="med-file-row">' +
                        '<input type="checkbox" class="med-file-check" value="' + f.name + '">' +
                        '<i class="fa ' + icon + '"></i> ' +
                        '<span class="med-file-name">' + f.name + '</span>' +
                        '<span class="med-file-size">' + sizeStr + '</span>' +
                    '</div>';
                });
                html += '</div>';

                // Analysis modes
                html += '<div class="med-modes-header">' + t('Analiza') + '</div>';
                html += '<div class="med-modes-grid">';
                var modes = [
                    { id: 'spellcheck', icon: 'fa-spell-check', name: t('Literowki'), color: '#f59e0b' },
                    { id: 'timeline', icon: 'fa-clock', name: t('Chronologia'), color: '#3b82f6' },
                    { id: 'summary', icon: 'fa-heartbeat', name: t('Skrot Holter/ECHO'), color: '#ef4444' },
                    { id: 'interactions', icon: 'fa-pills', name: t('Interakcje lekowe'), color: '#8b5cf6' },
                    { id: 'referral', icon: 'fa-file-medical', name: t('Skierowanie'), color: '#10b981' },
                    { id: 'clinical', icon: 'fa-stethoscope', name: t('Wskazania ESC'), color: '#06b6d4' },
                ];
                modes.forEach(function (m) {
                    html += '<button class="med-mode-card" data-mode="' + m.id + '" style="--mode-color:' + m.color + '">' +
                        '<i class="fa ' + m.icon + '"></i>' +
                        '<span>' + m.name + '</span>' +
                    '</button>';
                });
                html += '</div>';
            }

            // Jobs history
            html += '<div id="med-jobs-section"></div>';

            content.innerHTML = html;

            // Wire up events
            var fileInput = content.querySelector('#med-file-input');
            if (fileInput) {
                fileInput.addEventListener('change', function () {
                    if (fileInput.files.length) uploadFiles(name, fileInput.files);
                });
            }

            var delBtn = content.querySelector('#med-del-patient');
            if (delBtn) {
                delBtn.addEventListener('click', function () {
                    confirmDialog(t('Usunac folder pacjenta i wszystkie dokumenty?'), async function () {
                        await api('/med-assistant/patients/' + encodeURIComponent(name), { method: 'DELETE' });
                        currentPatient = null;
                        loadPatients();
                        content.innerHTML = '<div class="med-placeholder"><i class="fa fa-folder-open"></i><p>' + t('Wybierz lub utworz folder pacjenta') + '</p></div>';
                    });
                });
            }

            var selectAll = content.querySelector('#med-select-all');
            if (selectAll) {
                selectAll.addEventListener('change', function () {
                    content.querySelectorAll('.med-file-check').forEach(function (cb) {
                        cb.checked = selectAll.checked;
                    });
                });
            }

            content.querySelectorAll('.med-mode-card').forEach(function (btn) {
                btn.addEventListener('click', function () {
                    var mode = btn.getAttribute('data-mode');
                    runAnalysis(name, mode);
                });
            });

            loadJobs(name);
        }

        async function uploadFiles(patient, fileList) {
            var fd = new FormData();
            for (var i = 0; i < fileList.length; i++) {
                fd.append('files', fileList[i]);
            }
            var resp = await fetch('/api/med-assistant/patients/' + encodeURIComponent(patient) + '/upload', {
                method: 'POST',
                headers: {
                    'Authorization': 'Bearer ' + NAS.token,
                    'X-CSRFToken': NAS.csrfToken,
                },
                body: fd,
            });
            var data = await resp.json();
            if (data && data.ok) {
                showPatient(patient);
            } else if (data && data.error) {
                if (typeof showToast === 'function') showToast(data.error, 'error');
            }
        }

        async function runAnalysis(patient, mode) {
            var checks = content.querySelectorAll('.med-file-check:checked');
            var files = [];
            checks.forEach(function (cb) { files.push(cb.value); });
            if (files.length === 0) {
                if (typeof showToast === 'function') showToast(t('Zaznacz pliki do analizy'), 'warning');
                return;
            }

            var payload = { mode: mode, patient: patient, files: files };

            if (mode === 'referral') {
                var target = prompt(t('Skierowanie do jakiego specjalisty? (np. neurolog, kardiochirurg)'));
                if (!target) return;
                payload.referral_target = target;
            }

            // Show progress
            var jobsSection = content.querySelector('#med-jobs-section');
            if (jobsSection) {
                jobsSection.innerHTML =
                    '<div class="med-progress-box">' +
                        '<svg class="med-progress-ring" viewBox="0 0 60 60">' +
                            '<circle class="med-ring-bg" cx="30" cy="30" r="26"></circle>' +
                            '<circle class="med-ring-fg" id="med-ring" cx="30" cy="30" r="26"></circle>' +
                        '</svg>' +
                        '<span id="med-job-pct">0%</span>' +
                        '<p id="med-job-msg">' + t('Rozpoczynanie analizy...') + '</p>' +
                    '</div>';
                var ring = jobsSection.querySelector('#med-ring');
                if (ring) {
                    var circ = 2 * Math.PI * 26;
                    ring.style.strokeDasharray = circ;
                    ring.style.strokeDashoffset = circ;
                }
            }

            var data = await api('/med-assistant/analyze', {
                method: 'POST',
                body: payload,
            });
            if (data && data.error) {
                if (typeof showToast === 'function') showToast(data.error, 'error');
                if (currentPatient) showPatient(currentPatient);
            }
        }

        async function loadJobs(patient) {
            var section = content.querySelector('#med-jobs-section');
            if (!section) return;

            var data = await api('/med-assistant/jobs');
            if (!data || !data.items) return;

            var jobs = data.items.filter(function (j) { return j.patient === patient; });
            if (jobs.length === 0) return;

            var html = '<div class="med-jobs-header">' + t('Historia analiz') + '</div>';
            jobs.forEach(function (job) {
                var icon, cls;
                if (job.status === 'done') {
                    icon = '<i class="fa fa-check-circle"></i>';
                    cls = 'med-job-done';
                } else if (job.status === 'error') {
                    icon = '<i class="fa fa-times-circle"></i>';
                    cls = 'med-job-error';
                } else {
                    icon = '<i class="fa fa-spinner fa-spin"></i>';
                    cls = 'med-job-processing';
                }
                var dateStr = job.created_at ? new Date(job.created_at * 1000).toLocaleString() : '';
                html += '<div class="med-job-row ' + cls + '" data-job="' + job.job_id + '">' +
                    '<div class="med-job-info">' +
                        icon + ' ' +
                        '<span class="med-job-mode">' + (job.mode_name || job.mode) + '</span>' +
                        '<span class="med-job-date">' + dateStr + '</span>' +
                    '</div>' +
                    '<div class="med-job-actions">' +
                        (job.status === 'done' ?
                            '<button class="med-btn-icon" data-export="' + job.job_id + '" title="' + t('Eksportuj') + '"><i class="fa fa-file-export"></i></button>' : '') +
                        '<button class="med-btn-icon med-btn-icon-danger" data-del="' + job.job_id + '" title="' + t('Usun') + '"><i class="fa fa-trash"></i></button>' +
                    '</div>' +
                '</div>';
            });

            section.innerHTML = html;

            section.querySelectorAll('.med-job-row[data-job]').forEach(function (row) {
                row.addEventListener('click', function () {
                    showJobResult(row.getAttribute('data-job'));
                });
            });

            section.querySelectorAll('[data-export]').forEach(function (btn) {
                btn.addEventListener('click', function (e) {
                    e.stopPropagation();
                    exportJob(btn.getAttribute('data-export'));
                });
            });

            section.querySelectorAll('[data-del]').forEach(function (btn) {
                btn.addEventListener('click', function (e) {
                    e.stopPropagation();
                    var jid = btn.getAttribute('data-del');
                    confirmDialog(t('Usunac wynik analizy?'), async function () {
                        await api('/med-assistant/job/' + jid, { method: 'DELETE' });
                        if (currentPatient) showPatient(currentPatient);
                    });
                });
            });
        }

        async function showJobResult(jobId) {
            var data = await api('/med-assistant/job/' + jobId);
            if (!data || data.error) return;

            var resultHtml = '<div class="med-result">';
            resultHtml += '<div class="med-result-header">';
            resultHtml += '<h3>' + (data.mode_name || data.mode) + '</h3>';
            resultHtml += '<span class="med-result-meta">' +
                t('Pacjent') + ': ' + (data.patient || '') + ' | ' +
                t('Pliki') + ': ' + (data.files || []).join(', ') +
            '</span>';
            resultHtml += '</div>';

            if (data.result) {
                resultHtml += '<div class="med-result-text">' + _medMd(data.result) + '</div>';
            } else if (data.error) {
                resultHtml += '<div class="med-result-error">' + data.error + '</div>';
            }
            resultHtml += '</div>';

            createWindow('med-result-' + jobId, {
                title: (data.mode_name || 'Wynik') + ' - ' + (data.patient || ''),
                icon: 'fa-file-medical-alt',
                iconColor: '#06b6d4',
                width: 700, height: 550,
                onRender: function (b) { b.innerHTML = resultHtml; },
            });
        }

        async function exportJob(jobId) {
            var data = await api('/med-assistant/job/' + jobId + '/export');
            if (data && data.ok) {
                if (typeof showToast === 'function') showToast(t('Wyeksportowano jako') + ': ' + data.filename, 'success');
                if (currentPatient) showPatient(currentPatient);
            } else if (data && data.error) {
                if (typeof showToast === 'function') showToast(data.error, 'error');
            }
        }
    }
};
