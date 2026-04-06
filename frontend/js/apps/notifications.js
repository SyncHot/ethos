/* EthOS — Notification Channels App */

AppRegistry['notifications'] = function (appDef) {
    const _cl = (level, msg, details) => typeof NAS !== 'undefined' && NAS.logClient
        ? NAS.logClient('notifications', level, msg, details) : console.log('[notifications]', msg, details || '');

    const w = createWindow('notifications', {
        title: 'Powiadomienia',
        icon: 'fa-bell',
        iconColor: '#f59e0b',
        width: 920,
        height: 640,
    });
    const body = w.body;

    let state = { channels: {}, triggers: {}, history: [], testing: {} };

    const CHANNELS = {
        telegram: { label: 'Telegram',  icon: 'fa-paper-plane', color: '#26a5e4',
            fields: [
                { key: 'bot_token', label: 'Bot Token', type: 'password' },
                { key: 'chat_id',   label: 'Chat ID',   type: 'text' },
            ]},
        discord: { label: 'Discord', icon: 'fa-comment-dots', color: '#5865f2',
            fields: [
                { key: 'webhook_url', label: 'Webhook URL', type: 'password' },
            ]},
        gotify: { label: 'Gotify', icon: 'fa-server', color: '#2196f3',
            fields: [
                { key: 'server_url', label: 'Server URL', type: 'text', placeholder: 'https://gotify.example.com' },
                { key: 'app_token',  label: 'App Token',  type: 'password' },
            ]},
        ntfy: { label: 'ntfy', icon: 'fa-bell', color: '#57c4e5',
            fields: [
                { key: 'server_url', label: 'Server URL', type: 'text', placeholder: 'https://ntfy.sh' },
                { key: 'topic',      label: 'Topic',      type: 'text' },
            ]},
        smtp: { label: 'E-mail (SMTP)', icon: 'fa-envelope', color: '#ea4335',
            fields: [
                { key: 'host',      label: 'SMTP Host',  type: 'text' },
                { key: 'port',      label: 'Port',       type: 'number' },
                { key: 'username',  label: t('Użytkownik'), type: 'text' },
                { key: 'password',  label: t('Hasło'),      type: 'password' },
                { key: 'from_addr', label: t('Od (e-mail)'), type: 'text' },
                { key: 'to_addr',   label: t('Do (e-mail)'), type: 'text' },
                { key: 'use_tls',   label: 'TLS',         type: 'checkbox' },
            ]},
        webhook: { label: 'Webhook', icon: 'fa-globe', color: '#10b981',
            fields: [
                { key: 'url',    label: 'URL',    type: 'password' },
                { key: 'method', label: t('Metoda'), type: 'select', options: ['POST', 'PUT', 'PATCH'] },
            ]},
    };

    const TRIGGERS = {
        smart_warning:    { label: t('Ostrzeżenie S.M.A.R.T.'),    icon: 'fa-hdd' },
        backup_failed:    { label: t('Kopia zapasowa — błąd'),      icon: 'fa-exclamation-triangle' },
        backup_completed: { label: t('Kopia zapasowa — sukces'),    icon: 'fa-check-circle' },
        disk_full_90:     { label: t('Dysk zapełniony (>90%)'),     icon: 'fa-database' },
        login_failed:     { label: t('Nieudane logowanie'),         icon: 'fa-sign-in-alt' },
        container_crash:  { label: t('Awaria kontenera Docker'),    icon: 'fa-cubes' },
        update_available: { label: t('Dostępna aktualizacja'),      icon: 'fa-download' },
        raid_degraded:    { label: t('Degradacja macierzy RAID'),   icon: 'fa-layer-group' },
    };

    /* ── Tabs ───────────────────────────────────────────────── */

    const tabs = [
        { id: 'channels', label: t('Kanały'),       icon: 'fa-satellite-dish' },
        { id: 'triggers', label: t('Wyzwalacze'),   icon: 'fa-bolt' },
        { id: 'history',  label: t('Historia'),      icon: 'fa-history' },
    ];

    let activeTab = 'channels';

    /* ── Render ─────────────────────────────────────────────── */

    const render = () => {
        body.innerHTML = `
            <div class="notif-wrap">
                <div class="notif-sidebar">
                    ${tabs.map(t => `
                        <button class="notif-tab-btn ${activeTab === t.id ? 'active' : ''}" data-tab="${t.id}">
                            <i class="fas ${t.icon} notif-tab-icon"></i>
                            <span>${t.label}</span>
                        </button>
                    `).join('')}
                </div>
                <div class="notif-panel" id="notif-panel"></div>
            </div>
        `;

        body.querySelectorAll('.notif-tab-btn').forEach(btn => {
            btn.onclick = () => { activeTab = btn.dataset.tab; render(); };
        });

        const panel = body.querySelector('#notif-panel');
        if (activeTab === 'channels')  renderChannels(panel);
        else if (activeTab === 'triggers') renderTriggers(panel);
        else if (activeTab === 'history')  renderHistory(panel);
    };

    /* ── Channels tab ───────────────────────────────────────── */

    const renderChannels = (panel) => {
        panel.innerHTML = `
            <div class="notif-header">
                <span class="notif-title"><i class="fas fa-satellite-dish notif-icon-accent"></i> ${t('Kanały powiadomień')}</span>
                <span class="notif-spacer"></span>
                <button class="btn btn-sm btn-primary" id="notif-save-ch">
                    <i class="fas fa-save"></i> Zapisz
                </button>
            </div>
            <div class="notif-desc">${t('Skonfiguruj kanały, na które będą wysyłane powiadomienia systemowe.')}</div>
            <div class="notif-cards" id="notif-cards"></div>
        `;

        const container = panel.querySelector('#notif-cards');
        for (const [chName, chDef] of Object.entries(CHANNELS)) {
            const chState = state.channels[chName] || {};
            const card = document.createElement('div');
            card.className = 'notif-card';
            card.innerHTML = `
                <div class="notif-card-head">
                    <i class="fas ${chDef.icon}" style="color:${chDef.color}"></i>
                    <span class="notif-card-title">${chDef.label}</span>
                    <span class="notif-spacer"></span>
                    <label class="notif-toggle">
                        <input type="checkbox" data-ch="${chName}" data-field="enabled"
                            ${chState.enabled ? 'checked' : ''}>
                        <span class="notif-toggle-slider"></span>
                    </label>
                </div>
                <div class="notif-card-body ${chState.enabled ? '' : 'notif-disabled'}">
                    ${chDef.fields.map(f => fieldHtml(chName, f, chState)).join('')}
                    <div class="notif-card-actions">
                        <button class="btn btn-sm btn-outline notif-test-btn" data-ch="${chName}"
                            ${!chState.enabled ? 'disabled' : ''}>
                            <i class="fas ${state.testing[chName] ? 'fa-spinner fa-spin' : 'fa-vial'}"></i>
                            Test
                        </button>
                        <span class="notif-test-result" id="notif-result-${chName}"></span>
                    </div>
                </div>
            `;
            container.appendChild(card);
        }

        // Bind toggle → show/hide fields
        container.querySelectorAll('.notif-toggle input').forEach(inp => {
            inp.onchange = () => {
                const ch = inp.dataset.ch;
                if (!state.channels[ch]) state.channels[ch] = {};
                state.channels[ch].enabled = inp.checked;
                const cardBody = inp.closest('.notif-card').querySelector('.notif-card-body');
                cardBody.classList.toggle('notif-disabled', !inp.checked);
                const testBtn = inp.closest('.notif-card').querySelector('.notif-test-btn');
                testBtn.disabled = !inp.checked;
            };
        });

        // Bind field inputs
        container.querySelectorAll('[data-ch][data-field]').forEach(el => {
            if (el.closest('.notif-toggle')) return; // skip toggle
            const ch = el.dataset.ch;
            const field = el.dataset.field;
            const update = () => {
                if (!state.channels[ch]) state.channels[ch] = {};
                if (el.type === 'checkbox') {
                    state.channels[ch][field] = el.checked;
                } else {
                    state.channels[ch][field] = el.value;
                }
            };
            el.addEventListener('input', update);
            el.addEventListener('change', update);
        });

        // Test buttons
        container.querySelectorAll('.notif-test-btn').forEach(btn => {
            btn.onclick = async () => {
                const ch = btn.dataset.ch;
                state.testing[ch] = true;
                btn.innerHTML = '<i class="fas fa-spinner fa-spin"></i> ' + t('Wysyłanie…');
                btn.disabled = true;
                const resultEl = panel.querySelector(`#notif-result-${ch}`);
                resultEl.textContent = '';
                resultEl.className = 'notif-test-result';
                try {
                    await saveConfig();
                    const resp = await api('/notifications/test', {
                        method: 'POST', body: { channel: ch }
                    });
                    resultEl.textContent = t('✓ Wysłano');
                    resultEl.classList.add('notif-ok');
                } catch (e) {
                    resultEl.textContent = '✗ ' + (e.message || t('Błąd'));
                    resultEl.classList.add('notif-err');
                } finally {
                    state.testing[ch] = false;
                    btn.innerHTML = '<i class="fas fa-vial"></i> Test';
                    btn.disabled = false;
                }
            };
        });

        // Save button
        panel.querySelector('#notif-save-ch').onclick = async () => {
            await saveConfig();
            toast('Zapisano konfigurację kanałów', 'success');
        };
    };

    const fieldHtml = (chName, f, chState) => {
        const val = chState[f.key] ?? '';
        if (f.type === 'checkbox') {
            return `
                <label class="notif-field-row notif-checkbox-label">
                    <input type="checkbox" data-ch="${chName}" data-field="${f.key}" ${val ? 'checked' : ''}>
                    <span>${f.label}</span>
                </label>`;
        }
        if (f.type === 'select') {
            return `
                <div class="notif-field-row">
                    <label class="notif-field-label">${f.label}</label>
                    <select class="form-control notif-input" data-ch="${chName}" data-field="${f.key}">
                        ${f.options.map(o => `<option value="${o}" ${val === o ? 'selected' : ''}>${o}</option>`).join('')}
                    </select>
                </div>`;
        }
        return `
            <div class="notif-field-row">
                <label class="notif-field-label">${f.label}</label>
                <input class="form-control notif-input" type="${f.type === 'password' ? 'text' : f.type}"
                    data-ch="${chName}" data-field="${f.key}"
                    value="${_esc(String(val))}"
                    placeholder="${f.placeholder || ''}"
                    autocomplete="off">
            </div>`;
    };

    /* ── Triggers tab ───────────────────────────────────────── */

    const renderTriggers = (panel) => {
        panel.innerHTML = `
            <div class="notif-header">
                <span class="notif-title"><i class="fas fa-bolt notif-icon-accent"></i> Wyzwalacze</span>
                <span class="notif-spacer"></span>
                <button class="btn btn-sm btn-primary" id="notif-save-tr">
                    <i class="fas fa-save"></i> Zapisz
                </button>
            </div>
            <div class="notif-desc">${t('Wybierz, które zdarzenia mają generować powiadomienia.')}</div>
            <div class="notif-trigger-list" id="notif-triggers"></div>
        `;

        const list = panel.querySelector('#notif-triggers');
        for (const [trKey, trDef] of Object.entries(TRIGGERS)) {
            const enabled = state.triggers[trKey] ?? false;
            const row = document.createElement('div');
            row.className = 'notif-trigger-row';
            row.innerHTML = `
                <i class="fas ${trDef.icon} notif-trigger-icon"></i>
                <span class="notif-trigger-label">${trDef.label}</span>
                <span class="notif-spacer"></span>
                <label class="notif-toggle">
                    <input type="checkbox" data-trigger="${trKey}" ${enabled ? 'checked' : ''}>
                    <span class="notif-toggle-slider"></span>
                </label>
            `;
            list.appendChild(row);
        }

        list.querySelectorAll('[data-trigger]').forEach(inp => {
            inp.onchange = () => {
                state.triggers[inp.dataset.trigger] = inp.checked;
            };
        });

        panel.querySelector('#notif-save-tr').onclick = async () => {
            await saveConfig();
            toast('Zapisano wyzwalacze', 'success');
        };
    };

    /* ── History tab ────────────────────────────────────────── */

    const renderHistory = (panel) => {
        panel.innerHTML = `
            <div class="notif-header">
                <span class="notif-title"><i class="fas fa-history notif-icon-accent"></i> ${t('Historia powiadomień')}</span>
                <span class="notif-spacer"></span>
                <button class="btn btn-sm btn-outline" id="notif-refresh-hist">
                    <i class="fas fa-sync-alt"></i> ${t('Odśwież')}
                </button>
            </div>
            <div class="notif-history-wrap" id="notif-hist-body"></div>
        `;

        panel.querySelector('#notif-refresh-hist').onclick = async () => {
            await loadHistory();
            renderHistory(panel);
        };

        const wrap = panel.querySelector('#notif-hist-body');

        if (!state.history.length) {
            wrap.innerHTML = '<div class="notif-empty">' + t('Brak wysłanych powiadomień.') + '</div>';
            return;
        }

        let html = `<table class="notif-table">
            <thead><tr>
                <th>${t('Czas')}</th><th>${t('Kanał')}</th><th>${t('Tytuł')}</th><th>${t('Status')}</th>
            </tr></thead><tbody>`;
        for (const h of state.history) {
            const cls = h.success ? 'notif-ok' : 'notif-err';
            const status = h.success ? '✓ OK' : `✗ ${h.error || t('Błąd')}`;
            html += `<tr>
                <td class="notif-td-mono">${_esc(h.time)}</td>
                <td>${_esc(h.channel)}</td>
                <td>${_esc(h.title)}</td>
                <td class="${cls}">${_esc(status)}</td>
            </tr>`;
        }
        html += '</tbody></table>';
        wrap.innerHTML = html;
    };

    /* ── API helpers ────────────────────────────────────────── */

    const loadConfig = async () => {
        try {
            const data = await api('/notifications/config');
            state.channels = data.channels || {};
            state.triggers = data.triggers || {};
        } catch (e) {
            body.innerHTML = `<div class="p-3">${t('Błąd ładowania konfiguracji:')} ${e.message}</div>`;
        }
    };

    const loadHistory = async () => {
        try {
            state.history = await api('/notifications/history');
        } catch (_) {
            state.history = [];
        }
    };

    const saveConfig = async () => {
        // Collect current field values from DOM before saving
        const cards = body.querySelectorAll('#notif-cards [data-ch][data-field]');
        cards.forEach(el => {
            const ch = el.dataset.ch;
            const field = el.dataset.field;
            if (!state.channels[ch]) state.channels[ch] = {};
            if (el.type === 'checkbox') {
                state.channels[ch][field] = el.checked;
            } else {
                state.channels[ch][field] = el.value;
            }
        });
        await api('/notifications/config', {
            method: 'PUT',
            body: { channels: state.channels, triggers: state.triggers }
        });
    };

    const _esc = (s) => {
        const d = document.createElement('div');
        d.textContent = s;
        return d.innerHTML;
    };

    /* ── Init ───────────────────────────────────────────────── */

    const init = async () => {
        body.innerHTML = '<div class="notif-loading"><i class="fas fa-spinner fa-spin"></i> ' + t('Ładowanie…') + '</div>';
        await Promise.all([loadConfig(), loadHistory()]);
        render();
    };

    init();
};
