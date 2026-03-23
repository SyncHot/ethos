/* ── Cloud Backup (rclone) ─────────────────────────────────── */

AppRegistry['cloud-backup'] = function (appDef) {
    createWindow('cloud-backup', {
        title: t('Backup w chmurze'),
        icon: appDef.icon,
        iconColor: appDef.color,
        width: 1100,
        height: 750,
        onRender: (body) => renderCloudBackupApp(body),
    });
};

function renderCloudBackupApp(body) {
    const state = {
        tab: 'providers',
        providers: [],
        jobs: [],
        history: [],
        loading: false,
    };

    const PROVIDER_TYPES = {
        s3:     { label: 'Amazon S3 / MinIO', icon: 'fa-aws', fields: [
            { key: 'provider', label: 'Provider', placeholder: 'AWS/Minio/Other', type: 'select', options: ['AWS','Minio','DigitalOcean','Wasabi','Other'] },
            { key: 'access_key_id', label: 'Access Key ID', placeholder: 'AKIA...' },
            { key: 'secret_access_key', label: 'Secret Access Key', placeholder: '***', password: true },
            { key: 'region', label: 'Region', placeholder: 'us-east-1' },
            { key: 'endpoint', label: 'Endpoint (optional)', placeholder: 'https://s3.example.com' },
        ]},
        b2:     { label: 'Backblaze B2', icon: 'fa-cloud', fields: [
            { key: 'account', label: 'Account ID', placeholder: '00...' },
            { key: 'key', label: 'Application Key', placeholder: '***', password: true },
        ]},
        gdrive: { label: 'Google Drive', icon: 'fa-google-drive', fields: [
            { key: 'client_id', label: 'Client ID', placeholder: '...apps.googleusercontent.com' },
            { key: 'client_secret', label: 'Client Secret', placeholder: '***', password: true },
            { key: 'token', label: 'Token JSON', placeholder: '{"access_token":"..."}' },
        ]},
        webdav: { label: 'WebDAV', icon: 'fa-globe', fields: [
            { key: 'url', label: 'URL', placeholder: 'https://cloud.example.com/remote.php/dav/files/user' },
            { key: 'vendor', label: 'Vendor', type: 'select', options: ['nextcloud','owncloud','sharepoint','other'] },
            { key: 'user', label: 'Username', placeholder: 'user' },
            { key: 'pass', label: 'Password', placeholder: '***', password: true },
        ]},
        sftp:   { label: 'SFTP', icon: 'fa-server', fields: [
            { key: 'host', label: 'Host', placeholder: '192.168.1.100' },
            { key: 'port', label: 'Port', placeholder: '22' },
            { key: 'user', label: 'Username', placeholder: 'user' },
            { key: 'pass', label: 'Password', placeholder: '***', password: true },
            { key: 'key_file', label: 'Key file (optional)', placeholder: '/root/.ssh/id_rsa' },
        ]},
    };

    /* ── Render skeleton ─────────────────────────────────── */

    body.innerHTML = `
        <div class="cb-app">
            <div class="cb-sidebar">
                <nav class="cb-nav">
                    <a class="cb-nav-item active" data-tab="providers"><i class="fas fa-plug"></i> ${t('Dostawcy')}</a>
                    <a class="cb-nav-item" data-tab="jobs"><i class="fas fa-tasks"></i> ${t('Zadania')}</a>
                    <a class="cb-nav-item" data-tab="history"><i class="fas fa-history"></i> ${t('Historia')}</a>
                </nav>
            </div>
            <div class="cb-content">
                <div class="cb-panel active" id="cb-panel-providers"></div>
                <div class="cb-panel" id="cb-panel-jobs"></div>
                <div class="cb-panel" id="cb-panel-history"></div>
            </div>
        </div>
    `;

    const $ = sel => body.querySelector(sel);
    const $$ = sel => body.querySelectorAll(sel);

    /* ── Tab switching ───────────────────────────────────── */

    $$('.cb-nav-item').forEach(btn => {
        btn.onclick = () => {
            $$('.cb-nav-item').forEach(b => b.classList.remove('active'));
            btn.classList.add('active');
            $$('.cb-panel').forEach(p => p.classList.remove('active'));
            $(`#cb-panel-${btn.dataset.tab}`).classList.add('active');
            state.tab = btn.dataset.tab;
            renderTab();
        };
    });

    /* ── API helpers ─────────────────────────────────────── */

    async function cbApi(path, opts) { return api(`/cloud-backup${path}`, opts); }

    async function loadAll() {
        state.loading = true;
        try {
            const [prov, jobs, hist] = await Promise.all([
                cbApi('/providers'),
                cbApi('/jobs'),
                cbApi('/history?limit=100'),
            ]);
            state.providers = prov.remotes || [];
            state.jobs = jobs.jobs || [];
            state.history = hist.history || [];
        } catch (e) {
            toast(t('Błąd ładowania danych'), 'error');
        }
        state.loading = false;
        renderTab();
    }

    /* ── Render current tab ──────────────────────────────── */

    function renderTab() {
        if (state.tab === 'providers') renderProviders();
        else if (state.tab === 'jobs') renderJobs();
        else if (state.tab === 'history') renderHistory();
    }

    /* ── Providers tab ───────────────────────────────────── */

    function renderProviders() {
        const panel = $('#cb-panel-providers');
        const list = state.providers.map(p => `
            <div class="cb-card">
                <div class="cb-card-body">
                    <div class="cb-card-title"><i class="fas fa-cloud"></i> ${esc(p.name)}</div>
                    <span class="cb-badge">${esc(p.type || 'unknown')}</span>
                </div>
                <div class="cb-card-actions">
                    <button class="cb-btn cb-btn-danger cb-del-provider" data-name="${esc(p.name)}" title="${t('Usuń')}"><i class="fas fa-trash"></i></button>
                </div>
            </div>
        `).join('');

        panel.innerHTML = `
            <div class="cb-header">
                <h3><i class="fas fa-plug"></i> ${t('Dostawcy chmury')}</h3>
                <button class="cb-btn cb-btn-primary" id="cb-add-provider"><i class="fas fa-plus"></i> ${t('Dodaj')}</button>
            </div>
            ${state.providers.length === 0 ? `<div class="cb-empty"><i class="fas fa-cloud"></i><p>${t('Brak skonfigurowanych dostawców')}</p></div>` : `<div class="cb-card-list">${list}</div>`}
            <div id="cb-provider-form" style="display:none;"></div>
        `;

        panel.querySelector('#cb-add-provider').onclick = showProviderForm;
        panel.querySelectorAll('.cb-del-provider').forEach(btn => {
            btn.onclick = async () => {
                if (!confirm(t('Usunąć dostawcę') + ' ' + btn.dataset.name + '?')) return;
                await cbApi(`/providers/${encodeURIComponent(btn.dataset.name)}`, { method: 'DELETE' });
                toast(t('Usunięto'), 'success');
                loadAll();
            };
        });
    }

    function showProviderForm() {
        const form = $('#cb-provider-form');
        form.style.display = 'block';
        form.innerHTML = `
            <div class="cb-form-card">
                <h4>${t('Nowy dostawca')}</h4>
                <div class="cb-field">
                    <label>${t('Nazwa')}</label>
                    <input id="cb-prov-name" class="cb-input" placeholder="mys3" />
                </div>
                <div class="cb-field">
                    <label>${t('Typ')}</label>
                    <select id="cb-prov-type" class="cb-input">
                        ${Object.entries(PROVIDER_TYPES).map(([k, v]) => `<option value="${k}">${v.label}</option>`).join('')}
                    </select>
                </div>
                <div id="cb-prov-fields"></div>
                <div class="cb-form-actions">
                    <button class="cb-btn cb-btn-primary" id="cb-prov-save"><i class="fas fa-check"></i> ${t('Zapisz')}</button>
                    <button class="cb-btn" id="cb-prov-cancel">${t('Anuluj')}</button>
                </div>
            </div>
        `;

        const typeSelect = form.querySelector('#cb-prov-type');
        const renderFields = () => {
            const spec = PROVIDER_TYPES[typeSelect.value];
            form.querySelector('#cb-prov-fields').innerHTML = spec.fields.map(f => `
                <div class="cb-field">
                    <label>${f.label}</label>
                    ${f.type === 'select'
                        ? `<select class="cb-input" data-key="${f.key}">${f.options.map(o => `<option value="${o}">${o}</option>`).join('')}</select>`
                        : `<input class="cb-input" data-key="${f.key}" type="${f.password ? 'password' : 'text'}" placeholder="${f.placeholder || ''}" />`
                    }
                </div>
            `).join('');
        };
        typeSelect.onchange = renderFields;
        renderFields();

        form.querySelector('#cb-prov-cancel').onclick = () => { form.style.display = 'none'; };
        form.querySelector('#cb-prov-save').onclick = async () => {
            const name = form.querySelector('#cb-prov-name').value.trim();
            const type = typeSelect.value;
            const params = {};
            form.querySelectorAll('#cb-prov-fields [data-key]').forEach(el => {
                params[el.dataset.key] = el.value;
            });
            if (!name) { toast(t('Podaj nazwę'), 'warning'); return; }
            const res = await cbApi('/providers', { method: 'POST', body: { name, type, params } });
            if (res.error) { toast(res.error, 'error'); return; }
            toast(t('Dodano dostawcę'), 'success');
            form.style.display = 'none';
            loadAll();
        };
    }

    /* ── Jobs tab ────────────────────────────────────────── */

    function renderJobs() {
        const panel = $('#cb-panel-jobs');
        const list = state.jobs.map(j => {
            const statusCls = j.last_status === 'success' ? 'cb-status-ok' : j.last_status === 'error' ? 'cb-status-err' : 'cb-status-none';
            const statusLabel = j.running ? `<span class="cb-status-running"><i class="fas fa-spinner fa-spin"></i> ${t('Działa')}</span>` :
                j.last_status === 'success' ? `<span class="cb-status-ok">${t('OK')}</span>` :
                j.last_status === 'error' ? `<span class="cb-status-err">${t('Błąd')}</span>` :
                `<span class="cb-status-none">—</span>`;
            const schedLabel = j.schedule && j.schedule.enabled ? `<span class="cb-badge cb-badge-green">${j.schedule.cron}</span>` : `<span class="cb-badge">${t('ręcznie')}</span>`;

            return `
                <div class="cb-card">
                    <div class="cb-card-body">
                        <div class="cb-card-title">${esc(j.name)}</div>
                        <div class="cb-card-meta">
                            <span>${esc(j.source)} → ${esc(j.remote)}:${esc(j.remote_path || '/')}</span>
                        </div>
                        <div class="cb-card-badges">${statusLabel} ${schedLabel}</div>
                    </div>
                    <div class="cb-card-actions">
                        <button class="cb-btn cb-btn-primary cb-run-job" data-id="${j.id}" title="${t('Uruchom')}" ${j.running ? 'disabled' : ''}><i class="fas fa-play"></i></button>
                        <button class="cb-btn cb-restore-job" data-id="${j.id}" title="${t('Przywróć')}"><i class="fas fa-undo"></i></button>
                        <button class="cb-btn cb-edit-job" data-id="${j.id}" title="${t('Edytuj')}"><i class="fas fa-pen"></i></button>
                        <button class="cb-btn cb-btn-danger cb-del-job" data-id="${j.id}" title="${t('Usuń')}"><i class="fas fa-trash"></i></button>
                    </div>
                </div>
            `;
        }).join('');

        panel.innerHTML = `
            <div class="cb-header">
                <h3><i class="fas fa-tasks"></i> ${t('Zadania backupu')}</h3>
                <button class="cb-btn cb-btn-primary" id="cb-add-job"><i class="fas fa-plus"></i> ${t('Nowe zadanie')}</button>
            </div>
            ${state.jobs.length === 0 ? `<div class="cb-empty"><i class="fas fa-tasks"></i><p>${t('Brak zadań backupu')}</p></div>` : `<div class="cb-card-list">${list}</div>`}
            <div id="cb-job-form" style="display:none;"></div>
        `;

        panel.querySelector('#cb-add-job').onclick = () => showJobForm();
        panel.querySelectorAll('.cb-run-job').forEach(btn => {
            btn.onclick = async () => {
                const res = await cbApi(`/jobs/${btn.dataset.id}/run`, { method: 'POST' });
                if (res.error) { toast(res.error, 'error'); return; }
                toast(t('Backup uruchomiony'), 'info');
                setTimeout(loadAll, 1000);
            };
        });
        panel.querySelectorAll('.cb-restore-job').forEach(btn => {
            btn.onclick = async () => {
                if (!confirm(t('Przywrócić dane z kopii? Istniejące pliki mogą zostać nadpisane.'))) return;
                toast(t('Przywracanie...'), 'info');
                const res = await cbApi(`/jobs/${btn.dataset.id}/restore`, { method: 'POST', body: {} });
                if (res.error) { toast(res.error, 'error'); return; }
                toast(t('Przywrócono dane'), 'success');
            };
        });
        panel.querySelectorAll('.cb-edit-job').forEach(btn => {
            btn.onclick = () => {
                const job = state.jobs.find(j => j.id === btn.dataset.id);
                if (job) showJobForm(job);
            };
        });
        panel.querySelectorAll('.cb-del-job').forEach(btn => {
            btn.onclick = async () => {
                if (!confirm(t('Usunąć zadanie?'))) return;
                await cbApi(`/jobs/${btn.dataset.id}`, { method: 'DELETE' });
                toast(t('Usunięto'), 'success');
                loadAll();
            };
        });
    }

    function showJobForm(existing) {
        const form = $('#cb-job-form');
        form.style.display = 'block';
        const isEdit = !!existing;
        const j = existing || { name: '', source: '', remote: '', remote_path: '/', schedule: { enabled: false, cron: '0 2 * * *' }, retention: { enabled: false, days: 30 } };

        const remoteOptions = state.providers.map(p => `<option value="${esc(p.name)}" ${p.name === j.remote ? 'selected' : ''}>${esc(p.name)}</option>`).join('');

        form.innerHTML = `
            <div class="cb-form-card">
                <h4>${isEdit ? t('Edytuj zadanie') : t('Nowe zadanie')}</h4>
                <div class="cb-field"><label>${t('Nazwa')}</label><input id="cb-job-name" class="cb-input" value="${esc(j.name)}" /></div>
                <div class="cb-field"><label>${t('Ścieżka źródłowa')}</label><input id="cb-job-source" class="cb-input" placeholder="/mnt/data" value="${esc(j.source)}" /></div>
                <div class="cb-field-row">
                    <div class="cb-field"><label>${t('Dostawca (remote)')}</label><select id="cb-job-remote" class="cb-input">${remoteOptions}</select></div>
                    <div class="cb-field"><label>${t('Ścieżka zdalna')}</label><input id="cb-job-rpath" class="cb-input" placeholder="/" value="${esc(j.remote_path || '/')}" /></div>
                </div>
                <div class="cb-field-row">
                    <div class="cb-field">
                        <label><input type="checkbox" id="cb-job-sched-en" ${j.schedule.enabled ? 'checked' : ''} /> ${t('Harmonogram')}</label>
                        <input id="cb-job-cron" class="cb-input" placeholder="0 2 * * *" value="${esc(j.schedule.cron || '0 2 * * *')}" />
                    </div>
                    <div class="cb-field">
                        <label><input type="checkbox" id="cb-job-ret-en" ${j.retention && j.retention.enabled ? 'checked' : ''} /> ${t('Retencja (dni)')}</label>
                        <input id="cb-job-ret-days" class="cb-input" type="number" value="${j.retention ? j.retention.days : 30}" />
                    </div>
                </div>
                <div class="cb-form-actions">
                    <button class="cb-btn cb-btn-primary" id="cb-job-save"><i class="fas fa-check"></i> ${t('Zapisz')}</button>
                    <button class="cb-btn" id="cb-job-cancel">${t('Anuluj')}</button>
                </div>
            </div>
        `;

        form.querySelector('#cb-job-cancel').onclick = () => { form.style.display = 'none'; };
        form.querySelector('#cb-job-save').onclick = async () => {
            const payload = {
                name: form.querySelector('#cb-job-name').value.trim(),
                source: form.querySelector('#cb-job-source').value.trim(),
                remote: form.querySelector('#cb-job-remote').value,
                remote_path: form.querySelector('#cb-job-rpath').value.trim() || '/',
                schedule: {
                    enabled: form.querySelector('#cb-job-sched-en').checked,
                    cron: form.querySelector('#cb-job-cron').value.trim(),
                },
                retention: {
                    enabled: form.querySelector('#cb-job-ret-en').checked,
                    days: parseInt(form.querySelector('#cb-job-ret-days').value) || 30,
                },
            };
            if (!payload.name || !payload.source || !payload.remote) {
                toast(t('Wypełnij wymagane pola'), 'warning');
                return;
            }
            let res;
            if (isEdit) {
                res = await cbApi(`/jobs/${existing.id}`, { method: 'PUT', body: payload });
            } else {
                res = await cbApi('/jobs', { method: 'POST', body: payload });
            }
            if (res.error) { toast(res.error, 'error'); return; }
            toast(t('Zapisano'), 'success');
            form.style.display = 'none';
            loadAll();
        };
    }

    /* ── History tab ─────────────────────────────────────── */

    function renderHistory() {
        const panel = $('#cb-panel-history');
        if (state.history.length === 0) {
            panel.innerHTML = `
                <div class="cb-header"><h3><i class="fas fa-history"></i> ${t('Historia')}</h3></div>
                <div class="cb-empty"><i class="fas fa-history"></i><p>${t('Brak wpisów historii')}</p></div>
            `;
            return;
        }

        const rows = state.history.map(h => {
            const statusCls = h.status === 'success' ? 'cb-status-ok' : h.status === 'error' ? 'cb-status-err' : 'cb-status-running';
            const dur = h.duration ? `${h.duration}s` : '—';
            return `<tr>
                <td>${esc(h.job_name || h.job_id)}</td>
                <td>${h.started ? new Date(h.started).toLocaleString() : '—'}</td>
                <td>${dur}</td>
                <td><span class="${statusCls}">${esc(h.status)}</span></td>
                <td>${h.errors || 0}</td>
                <td class="cb-td-msg">${esc(h.message || '')}</td>
            </tr>`;
        }).join('');

        panel.innerHTML = `
            <div class="cb-header"><h3><i class="fas fa-history"></i> ${t('Historia')}</h3></div>
            <div class="cb-table-wrap">
                <table class="cb-table">
                    <thead><tr>
                        <th>${t('Zadanie')}</th><th>${t('Start')}</th><th>${t('Czas')}</th>
                        <th>${t('Status')}</th><th>${t('Błędy')}</th><th>${t('Wiadomość')}</th>
                    </tr></thead>
                    <tbody>${rows}</tbody>
                </table>
            </div>
        `;
    }

    /* ── Utility ─────────────────────────────────────────── */

    function esc(s) { const d = document.createElement('div'); d.textContent = s || ''; return d.innerHTML; }

    /* ── Init ────────────────────────────────────────────── */

    loadAll();
}
