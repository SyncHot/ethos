/* ═══════════════════════════════════════════════════════════
   EthOS — Harmonogram (Cron Manager)
   Manage scheduled tasks via crontab
   ═══════════════════════════════════════════════════════════ */

AppRegistry['cron'] = function (appDef) {

    const _cl = (level, msg, details) => typeof NAS !== 'undefined' && NAS.logClient
        ? NAS.logClient('cron', level, msg, details) : console.log('[cron]', msg, details || '');

    const win = createWindow('cron', {
        title: 'Harmonogram',
        icon: 'fa-clock',
        iconColor: '#6366f1',
        width: 960,
        height: 650,
        resizable: true,
        maximizable: true
    });

    const body = win.body;
    body.style.display = 'flex';
    body.style.flexDirection = 'column';
    body.style.background = 'var(--bg-default)';
    body.style.color = 'var(--text-default)';
    body.style.padding = '0';
    body.style.overflow = 'hidden';

    // ─── Header ───
    const header = document.createElement('div');
    header.className = 'cron-header';
    header.innerHTML = `
        <div>
            <h2 style="margin:0;display:flex;align-items:center;gap:10px">
                <i class="fas fa-clock" style="color:#6366f1"></i>
                ${t('Harmonogram zadań')}
            </h2>
            <div style="margin-top:4px;font-size:0.85em;opacity:0.7">${t('Zarządzanie zadaniami cron (root)')}</div>
        </div>
        <div style="display:flex;gap:8px">
            <button class="app-btn app-btn-sm" id="cron-refresh-btn">
                <i class="fas fa-sync-alt"></i> ${t('Odśwież')}
            </button>
            <button class="app-btn app-btn-sm app-btn-primary" id="cron-add-btn">
                <i class="fas fa-plus"></i> Dodaj zadanie
            </button>
        </div>
    `;
    body.appendChild(header);

    // ─── Content ───
    const content = document.createElement('div');
    content.className = 'cron-content';
    content.innerHTML = `
        <div id="cron-table-wrap" class="cron-table-wrap">
            <table class="app-table cron-table">
                <thead>
                    <tr>
                        <th>Harmonogram</th>
                        <th>Polecenie</th>
                        <th>Opis</th>
                        <th style="text-align:center">Status</th>
                        <th style="text-align:right">Akcje</th>
                    </tr>
                </thead>
                <tbody id="cron-tbody">
                    <tr><td colspan="5" class="cron-loading">${t('Ładowanie...')}</td></tr>
                </tbody>
            </table>
        </div>
    `;
    body.appendChild(content);

    // ─── Modal overlay (reused for add/edit) ───
    const overlay = document.createElement('div');
    overlay.className = 'cron-overlay';
    overlay.style.display = 'none';
    overlay.innerHTML = `
        <div class="cron-dialog">
            <div class="cron-dialog-header">
                <h3 id="cron-dlg-title" style="margin:0">Dodaj zadanie</h3>
                <button class="cron-dialog-close" id="cron-dlg-close">&times;</button>
            </div>
            <div class="cron-dialog-body">
                <label class="cron-label">Szybkie szablony</label>
                <div class="cron-presets" id="cron-presets">
                    <button class="app-btn app-btn-xs app-btn-secondary" data-preset="hourly">${t('Co godzinę')}</button>
                    <button class="app-btn app-btn-xs app-btn-secondary" data-preset="daily">${t('Codziennie o północy')}</button>
                    <button class="app-btn app-btn-xs app-btn-secondary" data-preset="weekly">${t('Co tydzień (pon)')}</button>
                    <button class="app-btn app-btn-xs app-btn-secondary" data-preset="monthly">${t('Co miesiąc (1-szy)')}</button>
                </div>
                <div class="cron-fields-grid">
                    <div>
                        <label class="cron-label">Minuta</label>
                        <input class="app-input cron-field-input" id="cron-f-minute" value="*" placeholder="0-59 lub *">
                    </div>
                    <div>
                        <label class="cron-label">Godzina</label>
                        <input class="app-input cron-field-input" id="cron-f-hour" value="*" placeholder="0-23 lub *">
                    </div>
                    <div>
                        <label class="cron-label">${t('Dzień mies.')}</label>
                        <input class="app-input cron-field-input" id="cron-f-dom" value="*" placeholder="1-31 lub *">
                    </div>
                    <div>
                        <label class="cron-label">${t('Miesiąc')}</label>
                        <input class="app-input cron-field-input" id="cron-f-month" value="*" placeholder="1-12 lub *">
                    </div>
                    <div>
                        <label class="cron-label">${t('Dzień tyg.')}</label>
                        <input class="app-input cron-field-input" id="cron-f-dow" value="*" placeholder="0-7 lub *">
                    </div>
                </div>
                <div id="cron-preview" class="cron-preview"></div>
                <label class="cron-label">Polecenie</label>
                <textarea class="app-input cron-command-input" id="cron-f-command" rows="3" placeholder="np. /usr/bin/rsync -a /src /dst"></textarea>
                <label class="cron-label">Opis (opcjonalny)</label>
                <input class="app-input" id="cron-f-desc" placeholder="Kopia zapasowa danych" style="width:100%;box-sizing:border-box">
            </div>
            <div class="cron-dialog-footer">
                <button class="app-btn" id="cron-dlg-cancel">Anuluj</button>
                <button class="app-btn app-btn-primary" id="cron-dlg-save">Zapisz</button>
            </div>
        </div>
    `;
    body.appendChild(overlay);

    // ════════════════════════ Logic ════════════════════════

    let editingIndex = null;  // null = add mode, number = edit mode

    // ── Schedule to human-readable ──
    function describeSchedule(m, h, dom, mon, dow) {
        if (m === '@reboot') return 'Przy starcie systemu';
        if (m === '@hourly')  return t('Co godzinę');
        if (m === '@daily' || m === '@midnight') return t('Codziennie o północy');
        if (m === '@weekly')  return t('Co tydzień (niedziela)');
        if (m === '@monthly') return t('Co miesiąc (1-szy)');
        if (m === '@yearly' || m === '@annually') return 'Raz w roku (1 sty)';

        const parts = [];

        // Minute + Hour
        if (m === '*' && h === '*') {
            parts.push(t('Co minutę'));
        } else if (h === '*' && m !== '*') {
            parts.push(`${t('Co godzinę o :')}${m.padStart(2, '0')}`);
        } else if (m === '0' && h === '*') {
            parts.push(t('Co godzinę'));
        } else if (h !== '*' && m !== '*') {
            if (m.includes(',') || m.includes('-') || m.includes('/')) {
                parts.push(`O ${h}:xx (min: ${m})`);
            } else {
                parts.push(`O ${h.padStart(2, '0')}:${m.padStart(2, '0')}`);
            }
        } else {
            parts.push(`Min: ${m}, Godz: ${h}`);
        }

        // Day of month
        if (dom !== '*') parts.push(`dnia ${dom}`);

        // Month
        const monthNames = ['', t('sty'),t('lut'),t('mar'),t('kwi'),t('maj'),t('cze'),t('lip'),t('sie'),t('wrz'),t('paź'),t('lis'),t('gru')];
        if (mon !== '*') {
            const mNum = parseInt(mon);
            parts.push(mNum >= 1 && mNum <= 12 ? monthNames[mNum] : `mies. ${mon}`);
        }

        // Day of week
        const dowNames = [t('niedz.'),t('pon.'),t('wt.'),t('śr.'),t('czw.'),t('pt.'),t('sob.'),t('niedz.')];
        if (dow !== '*') {
            const dNum = parseInt(dow);
            if (dNum >= 0 && dNum <= 7) {
                parts.push(dowNames[dNum]);
            } else {
                parts.push(t('dzień tyg.') + ` ${dow}`);
            }
        }

        return parts.join(', ');
    }

    function cronExpression(m, h, dom, mon, dow) {
        if (typeof m === 'string' && m.startsWith('@')) return m;
        return `${m} ${h} ${dom} ${mon} ${dow}`;
    }

    // ── Load & render ──
    async function loadJobs() {
        const tbody = document.getElementById('cron-tbody');
        tbody.innerHTML = '<tr><td colspan="5" class="cron-loading">' + t('Ładowanie...') + '</td></tr>';
        try {
            const res = await api('/cron/jobs');
            tbody.innerHTML = '';
            if (!res.jobs || res.jobs.length === 0) {
                tbody.innerHTML = `<tr><td colspan="5" class="cron-empty">
                    <i class="fas fa-calendar-check"></i>
                    ${t('Brak zaplanowanych zadań')}
                </td></tr>`;
                return;
            }
            res.jobs.forEach(job => {
                const tr = document.createElement('tr');
                const humanSchedule = describeSchedule(job.minute, job.hour, job.dom, job.month, job.dow);
                const rawExpr = cronExpression(job.minute, job.hour, job.dom, job.month, job.dow);
                tr.style.opacity = job.enabled ? '1' : '0.5';
                tr.innerHTML = `
                    <td>
                        <div>${esc(humanSchedule)}</div>
                        <div class="cron-schedule">${esc(rawExpr)}</div>
                    </td>
                    <td><div class="cron-cmd" title="${esc(job.command)}">${esc(job.command)}</div></td>
                    <td>${esc(job.description || '—')}</td>
                    <td style="text-align:center">
                        <label class="cron-toggle">
                            <input type="checkbox" ${job.enabled ? 'checked' : ''} data-idx="${job.index}">
                            <span class="cron-toggle-slider"></span>
                        </label>
                    </td>
                    <td style="text-align:right;white-space:nowrap">
                        <button class="app-btn app-btn-xs" data-edit="${job.index}"
                                data-minute="${esc(job.minute)}" data-hour="${esc(job.hour)}"
                                data-dom="${esc(job.dom)}" data-month="${esc(job.month)}"
                                data-dow="${esc(job.dow)}" data-command="${esc(job.command)}"
                                data-desc="${esc(job.description)}" data-enabled="${job.enabled}">
                            <i class="fas fa-pen"></i>
                        </button>
                        <button class="app-btn app-btn-xs app-btn-danger" data-del="${job.index}">
                            <i class="fas fa-trash"></i>
                        </button>
                    </td>
                `;
                tbody.appendChild(tr);
            });

            // Bind toggle switches
            tbody.querySelectorAll('input[data-idx]').forEach(cb => {
                cb.onchange = () => toggleJob(parseInt(cb.dataset.idx));
            });
            // Bind edit buttons
            tbody.querySelectorAll('button[data-edit]').forEach(btn => {
                btn.onclick = () => openEdit(btn);
            });
            // Bind delete buttons
            tbody.querySelectorAll('button[data-del]').forEach(btn => {
                btn.onclick = () => deleteJob(parseInt(btn.dataset.del));
            });
        } catch (e) {
            tbody.innerHTML = `<tr><td colspan="5" class="cron-loading" style="color:#ef4444">${t('Błąd:')} ${e.message}</td></tr>`;
        }
    }

    function esc(s) {
        if (!s && s !== 0) return '';
        const d = document.createElement('div');
        d.textContent = String(s);
        return d.innerHTML;
    }

    // ── Toggle enable/disable ──
    async function toggleJob(idx) {
        try {
            const res = await api(`/cron/jobs/${idx}/toggle`, { method: 'POST' });
            if (res.success) {
                toast(res.message, 'success');
                loadJobs();
            } else {
                toast(res.error || t('Błąd'), 'error');
            }
        } catch (e) { toast(e.message, 'error'); }
    }

    // ── Delete ──
    async function deleteJob(idx) {
        if (!await confirmDialog(t('Czy na pewno usunąć to zadanie?'))) return;
        try {
            const res = await api(`/cron/jobs/${idx}`, { method: 'DELETE' });
            if (res.success) {
                toast('Zadanie usunięte', 'success');
                loadJobs();
            } else {
                toast(res.error || t('Błąd'), 'error');
            }
        } catch (e) { toast(e.message, 'error'); }
    }

    // ── Dialog helpers ──
    function openDialog(title) {
        document.getElementById('cron-dlg-title').textContent = title;
        overlay.style.display = 'flex';
    }
    function closeDialog() {
        overlay.style.display = 'none';
        editingIndex = null;
    }

    function openAdd() {
        editingIndex = null;
        document.getElementById('cron-f-minute').value = '0';
        document.getElementById('cron-f-hour').value = '*';
        document.getElementById('cron-f-dom').value = '*';
        document.getElementById('cron-f-month').value = '*';
        document.getElementById('cron-f-dow').value = '*';
        document.getElementById('cron-f-command').value = '';
        document.getElementById('cron-f-desc').value = '';
        updatePreview();
        openDialog('Dodaj zadanie');
    }

    function openEdit(btn) {
        editingIndex = parseInt(btn.dataset.edit);
        document.getElementById('cron-f-minute').value = btn.dataset.minute;
        document.getElementById('cron-f-hour').value = btn.dataset.hour;
        document.getElementById('cron-f-dom').value = btn.dataset.dom;
        document.getElementById('cron-f-month').value = btn.dataset.month;
        document.getElementById('cron-f-dow').value = btn.dataset.dow;
        document.getElementById('cron-f-command').value = btn.dataset.command;
        document.getElementById('cron-f-desc').value = btn.dataset.desc || '';
        updatePreview();
        openDialog('Edytuj zadanie');
    }

    function updatePreview() {
        const m = document.getElementById('cron-f-minute').value.trim() || '*';
        const h = document.getElementById('cron-f-hour').value.trim() || '*';
        const dom = document.getElementById('cron-f-dom').value.trim() || '*';
        const mon = document.getElementById('cron-f-month').value.trim() || '*';
        const dow = document.getElementById('cron-f-dow').value.trim() || '*';
        const el = document.getElementById('cron-preview');
        el.innerHTML = `<i class="fas fa-info-circle"></i> ${esc(describeSchedule(m, h, dom, mon, dow))} &nbsp; <code>${esc(cronExpression(m, h, dom, mon, dow))}</code>`;
    }

    // ── Presets ──
    const presets = {
        hourly:  { minute: '0',  hour: '*', dom: '*', month: '*', dow: '*' },
        daily:   { minute: '0',  hour: '0', dom: '*', month: '*', dow: '*' },
        weekly:  { minute: '0',  hour: '0', dom: '*', month: '*', dow: '1' },
        monthly: { minute: '0',  hour: '0', dom: '1', month: '*', dow: '*' },
    };

    document.getElementById('cron-presets').addEventListener('click', e => {
        const btn = e.target.closest('[data-preset]');
        if (!btn) return;
        const p = presets[btn.dataset.preset];
        if (!p) return;
        document.getElementById('cron-f-minute').value = p.minute;
        document.getElementById('cron-f-hour').value = p.hour;
        document.getElementById('cron-f-dom').value = p.dom;
        document.getElementById('cron-f-month').value = p.month;
        document.getElementById('cron-f-dow').value = p.dow;
        updatePreview();
    });

    // Live preview on field change
    ['cron-f-minute','cron-f-hour','cron-f-dom','cron-f-month','cron-f-dow'].forEach(id => {
        document.getElementById(id).addEventListener('input', updatePreview);
    });

    // ── Save (create or update) ──
    async function saveJob() {
        const payload = {
            minute:  document.getElementById('cron-f-minute').value.trim() || '*',
            hour:    document.getElementById('cron-f-hour').value.trim() || '*',
            dom:     document.getElementById('cron-f-dom').value.trim() || '*',
            month:   document.getElementById('cron-f-month').value.trim() || '*',
            dow:     document.getElementById('cron-f-dow').value.trim() || '*',
            command: document.getElementById('cron-f-command').value.trim(),
            description: document.getElementById('cron-f-desc').value.trim(),
        };

        if (!payload.command) {
            toast('Podaj polecenie', 'error');
            return;
        }

        try {
            let res;
            if (editingIndex !== null) {
                payload.enabled = true;
                res = await api(`/cron/jobs/${editingIndex}`, { method: 'PUT', body: payload });
            } else {
                res = await api('/cron/jobs', { method: 'POST', body: payload });
            }
            if (res.success) {
                toast(res.message, 'success');
                closeDialog();
                loadJobs();
            } else {
                toast(res.error || t('Błąd'), 'error');
            }
        } catch (e) { toast(e.message, 'error'); }
    }

    // ── Bind buttons ──
    document.getElementById('cron-add-btn').onclick = openAdd;
    document.getElementById('cron-refresh-btn').onclick = loadJobs;
    document.getElementById('cron-dlg-close').onclick = closeDialog;
    document.getElementById('cron-dlg-cancel').onclick = closeDialog;
    document.getElementById('cron-dlg-save').onclick = saveJob;
    overlay.addEventListener('click', e => { if (e.target === overlay) closeDialog(); });

    // ── Initial load ──
    loadJobs();
};
